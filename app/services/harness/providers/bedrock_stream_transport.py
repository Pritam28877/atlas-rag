"""Bounded async bridge over blocking Bedrock ConverseStream."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Never

from app.services.harness.protocol import ProviderStreamEvent
from app.services.harness.providers.bedrock_boto_client import (
    BedrockRuntimeClientFactory,
    response_event_stream,
)
from app.services.harness.providers.bedrock_contracts import (
    CompiledBedrockConverseStreamRequest,
)
from app.services.harness.providers.bedrock_credentials import (
    BedrockCredentialMaterial,
)
from app.services.harness.providers.bedrock_decode_support import (
    validate_bedrock_wire_event,
)
from app.services.harness.providers.bedrock_decoder import (
    BedrockConverseStreamDecoder,
)
from app.services.harness.providers.bedrock_policy import (
    AuthorizedBedrockRoute,
)

MAXIMUM_BEDROCK_TRANSPORT_QUEUE = 32


class BedrockTransportErrorCode(StrEnum):
    CANCELLED = "cancelled"
    CLOSED = "closed"
    DEADLINE = "deadline"
    PROVIDER = "provider"
    REQUEST = "request"


class BedrockTransportError(RuntimeError):
    def __init__(self, code: BedrockTransportErrorCode) -> None:
        super().__init__("Bedrock transport failed")
        self.code = code


class _WorkerCompleted:
    pass


class _WorkerFailed:
    pass


class _ResponseMetadata:
    def __init__(self, content_sha256: str) -> None:
        self.content_sha256 = content_sha256


type _WorkerItem = (
    dict[str, object] | _ResponseMetadata | _WorkerCompleted | _WorkerFailed
)


class BoundedBedrockConverseTransport:
    def __init__(
        self,
        client_factory: BedrockRuntimeClientFactory,
        *,
        clock: Callable[[], datetime],
        maximum_concurrent_streams: int = 2,
    ) -> None:
        if not 1 <= maximum_concurrent_streams <= 8:
            raise ValueError("Bedrock stream concurrency is invalid")
        self._client_factory = client_factory
        self._clock = clock
        self._executor = ThreadPoolExecutor(
            max_workers=maximum_concurrent_streams,
            thread_name_prefix="atlas-bedrock-stream",
        )
        self._capacity = asyncio.Semaphore(maximum_concurrent_streams)
        self._state_lock = threading.Lock()
        self._active_stops: set[threading.Event] = set()
        self._closed = False

    async def stream(
        self,
        request: CompiledBedrockConverseStreamRequest,
        route: AuthorizedBedrockRoute,
        credential: BedrockCredentialMaterial,
        decoder: BedrockConverseStreamDecoder,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> AsyncIterator[ProviderStreamEvent]:
        with self._state_lock:
            closed = self._closed
        if closed:
            credential.zero()
            self._reject(BedrockTransportErrorCode.CLOSED)
        if request.model_id != route.model_id:
            credential.zero()
            self._reject(BedrockTransportErrorCode.REQUEST)
        try:
            remaining = self._remaining(deadline_at)
        except BaseException:
            credential.zero()
            raise
        timeout_seconds = max(1, min(300, int(remaining + 0.999)))
        queue: asyncio.Queue[_WorkerItem] = asyncio.Queue(
            maxsize=MAXIMUM_BEDROCK_TRANSPORT_QUEUE
        )
        stop = threading.Event()
        loop = asyncio.get_running_loop()
        try:
            await _acquire_transport_capacity(
                self._capacity,
                cancellation,
                remaining,
            )
        except BaseException:
            credential.zero()
            raise
        worker: concurrent.futures.Future[None] | None = None
        capacity_owned = True
        try:
            with self._state_lock:
                if self._closed:
                    self._reject(BedrockTransportErrorCode.CLOSED)
                self._active_stops.add(stop)
                try:
                    worker = self._executor.submit(
                        self._run_worker,
                        loop,
                        queue,
                        stop,
                        request,
                        route,
                        credential,
                        timeout_seconds,
                    )
                except BaseException:
                    self._active_stops.discard(stop)
                    raise
            worker.add_done_callback(
                lambda completed: self._worker_finished(
                    completed,
                    loop,
                    stop,
                )
            )
            capacity_owned = False
            try:
                async for event in self._consume(
                    queue,
                    decoder,
                    cancellation,
                    deadline_at,
                    stop,
                ):
                    yield event
            finally:
                stop.set()
        except BaseException:
            if worker is None:
                credential.zero()
            raise
        finally:
            if capacity_owned:
                self._capacity.release()

    async def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            active_stops = tuple(self._active_stops)
        for stop in active_stops:
            stop.set()
        await asyncio.to_thread(
            self._executor.shutdown,
            wait=True,
            cancel_futures=True,
        )

    def _worker_finished(
        self,
        worker: concurrent.futures.Future[None],
        loop: asyncio.AbstractEventLoop,
        stop: threading.Event,
    ) -> None:
        _consume_worker_exception(worker)
        with self._state_lock:
            self._active_stops.discard(stop)
        try:
            loop.call_soon_threadsafe(self._capacity.release)
        except RuntimeError:
            # The owning event loop is already closed, so its semaphore is dead.
            pass

    async def _consume(
        self,
        queue: asyncio.Queue[_WorkerItem],
        decoder: BedrockConverseStreamDecoder,
        cancellation: asyncio.Event,
        deadline_at: datetime,
        stop: threading.Event,
    ) -> AsyncIterator[ProviderStreamEvent]:
        while True:
            if cancellation.is_set():
                stop.set()
                self._reject(BedrockTransportErrorCode.CANCELLED)
            remaining = self._remaining(deadline_at)
            item = await _next_worker_item(
                queue,
                cancellation,
                remaining,
                stop,
            )
            if isinstance(item, _WorkerCompleted):
                return
            if isinstance(item, _WorkerFailed):
                self._reject(BedrockTransportErrorCode.PROVIDER)
            if isinstance(item, _ResponseMetadata):
                decoder.record_response_metadata_sha256(item.content_sha256)
                continue
            for event in decoder.decode(item):
                yield event

    def _run_worker(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[_WorkerItem],
        stop: threading.Event,
        request: CompiledBedrockConverseStreamRequest,
        route: AuthorizedBedrockRoute,
        credential: BedrockCredentialMaterial,
        timeout_seconds: int,
    ) -> None:
        client = None
        stream = None
        failed = False
        try:
            client = self._client_factory.create(
                route,
                credential,
                timeout_seconds=timeout_seconds,
            )
            response = client.converse_stream(**request.to_boto_request())
            envelope = response_event_stream(response)
            stream = envelope.stream
            if envelope.response_metadata_sha256 is not None:
                _put_worker_item(
                    loop,
                    queue,
                    stop,
                    _ResponseMetadata(envelope.response_metadata_sha256),
                )
            for event in stream:
                if stop.is_set():
                    break
                validate_bedrock_wire_event(event)
                _put_worker_item(loop, queue, stop, event)
        except Exception:
            failed = True
        finally:
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                failed = True
            try:
                if client is not None:
                    client.close()
            except Exception:
                failed = True
            finally:
                credential.zero()
        item: _WorkerItem = _WorkerFailed() if failed else _WorkerCompleted()
        _put_worker_item(loop, queue, stop, item)

    def _remaining(self, deadline_at: datetime) -> float:
        now = self._clock()
        if (
            now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or deadline_at.tzinfo is None
            or deadline_at.utcoffset() != timedelta(0)
            or deadline_at <= now
        ):
            self._reject(BedrockTransportErrorCode.DEADLINE)
        return (deadline_at - now).total_seconds()

    @staticmethod
    def _reject(code: BedrockTransportErrorCode) -> Never:
        raise BedrockTransportError(code)


def _put_worker_item(
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[_WorkerItem],
    stop: threading.Event,
    item: _WorkerItem,
) -> None:
    while not stop.is_set():
        future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        try:
            future.result(timeout=0.25)
            return
        except concurrent.futures.TimeoutError:
            future.cancel()


async def _next_worker_item(
    queue: asyncio.Queue[_WorkerItem],
    cancellation: asyncio.Event,
    remaining: float,
    stop: threading.Event,
) -> _WorkerItem:
    queue_task = asyncio.create_task(queue.get())
    cancellation_task = asyncio.create_task(cancellation.wait())
    try:
        done, _ = await asyncio.wait(
            (queue_task, cancellation_task),
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_task in done:
            stop.set()
            raise BedrockTransportError(BedrockTransportErrorCode.CANCELLED)
        if queue_task not in done:
            stop.set()
            raise BedrockTransportError(BedrockTransportErrorCode.DEADLINE)
        return queue_task.result()
    finally:
        for task in (queue_task, cancellation_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            queue_task,
            cancellation_task,
            return_exceptions=True,
        )


async def _acquire_transport_capacity(
    capacity: asyncio.Semaphore,
    cancellation: asyncio.Event,
    remaining: float,
) -> None:
    capacity_task = asyncio.create_task(capacity.acquire())
    cancellation_task = asyncio.create_task(cancellation.wait())
    acquired = False
    try:
        done, _ = await asyncio.wait(
            (capacity_task, cancellation_task),
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        acquired = capacity_task in done and capacity_task.result()
        if cancellation_task in done:
            if acquired:
                capacity.release()
            raise BedrockTransportError(BedrockTransportErrorCode.CANCELLED)
        if capacity_task not in done:
            raise BedrockTransportError(BedrockTransportErrorCode.DEADLINE)
    except asyncio.CancelledError:
        if acquired:
            capacity.release()
        raise
    finally:
        for task in (capacity_task, cancellation_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            capacity_task,
            cancellation_task,
            return_exceptions=True,
        )


def _consume_worker_exception(
    worker: concurrent.futures.Future[None],
) -> None:
    if not worker.cancelled():
        worker.exception()
