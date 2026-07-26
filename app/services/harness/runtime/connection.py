"""Transport-neutral ownership for one authenticated local connection."""

from __future__ import annotations

import asyncio
import time
from enum import StrEnum
from typing import Literal, Never, Protocol

from app.services.harness.protocol import (
    CommandEnvelope,
    CommandResponseKind,
    Payload,
    RequestId,
    StrictProtocolModel,
)
from app.services.harness.runtime.admission import (
    AdmissionError,
    AdmissionErrorCode,
    RequestAdmission,
    decode_command,
)
from app.services.harness.runtime.authority import (
    AuthenticatedCommandContext,
    AuthorityDeniedError,
    CommandAuthorityBinder,
)
from app.services.harness.runtime.framing import FrameDecoder, encode_frame
from app.services.harness.runtime.peer_auth import VerifiedPeerSession


class ConnectionErrorCode(StrEnum):
    MALFORMED_COMMAND = "malformed_command"
    AUTHORITY_DENIED = "authority_denied"
    CANCELLED = "cancelled"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    INTERNAL_ERROR = "internal_error"


class CommandReply(StrictProtocolModel):
    status: Literal["success"] = "success"
    request_id: RequestId
    response_kind: CommandResponseKind
    payload: Payload


class CommandErrorReply(StrictProtocolModel):
    status: Literal["error"] = "error"
    request_id: RequestId | None
    error_code: ConnectionErrorCode


class ConnectionClosedError(RuntimeError):
    """Raised when work is attempted after transport ownership ends."""


class ConnectionFatalError(RuntimeError):
    """Unexpected server failure that requires closing the connection."""


class CommandDispatcher(Protocol):
    async def dispatch(
        self,
        context: AuthenticatedCommandContext,
        cancellation_event: asyncio.Event,
    ) -> Payload: ...


class FrameSender(Protocol):
    async def __call__(self, frame: bytes) -> None: ...


class LocalConnection:
    """Serializes reads and owns all request tasks until completion or cleanup."""

    def __init__(
        self,
        *,
        session: VerifiedPeerSession,
        authority: CommandAuthorityBinder,
        admission: RequestAdmission,
        dispatcher: CommandDispatcher,
        maximum_frame_bytes: int,
        request_timeout_seconds: float,
    ) -> None:
        if not 0.001 <= request_timeout_seconds <= 3_600:
            raise ValueError(
                "request_timeout_seconds must be between 1 ms and 1 hour"
            )
        self._session = session
        self._authority = authority
        self._admission = admission
        self._dispatcher = dispatcher
        self._maximum_frame_bytes = maximum_frame_bytes
        self._request_timeout_seconds = request_timeout_seconds
        self._decoder = FrameDecoder(maximum_frame_bytes=maximum_frame_bytes)
        self._cancellation_event = asyncio.Event()
        self._receive_lock = asyncio.Lock()
        self._closed = False

    async def receive(self, chunk: bytes, sender: FrameSender) -> None:
        async with self._receive_lock:
            self._require_open()
            try:
                for payload in self._decoder.feed(chunk):
                    await self._handle_payload(payload, sender)
            except asyncio.CancelledError:
                self._abort()
                raise
            except Exception:
                self._abort()
                raise

    async def close(self) -> None:
        self._closed = True
        self._cancellation_event.set()
        async with self._receive_lock:
            self._decoder.reset()

    async def _handle_payload(
        self,
        payload: bytes,
        sender: FrameSender,
    ) -> None:
        try:
            envelope = decode_command(payload)
        except AdmissionError:
            await self._send_error(
                sender,
                request_id=None,
                code=ConnectionErrorCode.MALFORMED_COMMAND,
            )
            return

        deadline = time.monotonic() + self._request_timeout_seconds
        try:
            context, response_payload = await self._execute(envelope, deadline)
        except AuthorityDeniedError:
            await self._send_error(
                sender,
                request_id=envelope.request_id,
                code=ConnectionErrorCode.AUTHORITY_DENIED,
            )
            return
        except AdmissionError as error:
            await self._send_admission_error(sender, envelope.request_id, error)
            return
        except ConnectionClosedError:
            raise
        except Exception as error:
            await self._fail_fatal(sender, envelope.request_id, error)
        try:
            reply = CommandReply(
                request_id=envelope.request_id,
                response_kind=context.contract.response,
                payload=response_payload,
            )
            await self._send(sender, reply.model_dump_json().encode())
        except Exception as error:
            raise ConnectionFatalError("connection response failed") from error

    async def _execute(
        self,
        envelope: CommandEnvelope,
        deadline_monotonic: float,
    ) -> tuple[AuthenticatedCommandContext, Payload]:
        work_task = asyncio.create_task(
            self._dispatch(envelope, deadline_monotonic)
        )
        cancellation_task = asyncio.create_task(self._cancellation_event.wait())
        remaining_seconds = max(0.0, deadline_monotonic - time.monotonic())
        try:
            done, _ = await asyncio.wait(
                {work_task, cancellation_task},
                timeout=remaining_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task in done and cancellation_task.result():
                await self._cancel_and_wait(work_task)
                raise ConnectionClosedError("connection closed")
            if work_task in done:
                return work_task.result()
            await self._cancel_and_wait(work_task)
            raise AdmissionError(
                AdmissionErrorCode.DEADLINE_EXCEEDED,
                "request deadline elapsed during execution",
            )
        except asyncio.CancelledError:
            await self._cancel_and_wait(work_task)
            raise
        finally:
            await self._cancel_and_wait(cancellation_task)

    async def _dispatch(
        self,
        envelope: CommandEnvelope,
        deadline_monotonic: float,
    ) -> tuple[AuthenticatedCommandContext, Payload]:
        lease = await self._admission.acquire(
            deadline_monotonic=deadline_monotonic,
            cancellation_event=self._cancellation_event,
        )
        async with lease:
            context = await self._authority.bind(self._session, envelope)
            payload = await self._dispatcher.dispatch(
                context,
                self._cancellation_event,
            )
            return context, payload

    async def _send_admission_error(
        self,
        sender: FrameSender,
        request_id: RequestId,
        error: AdmissionError,
    ) -> None:
        if error.code is AdmissionErrorCode.CANCELLED:
            code = ConnectionErrorCode.CANCELLED
        elif error.code is AdmissionErrorCode.DEADLINE_EXCEEDED:
            code = ConnectionErrorCode.DEADLINE_EXCEEDED
        else:
            raise ConnectionFatalError("server generated an invalid deadline")
        await self._send_error(sender, request_id=request_id, code=code)

    async def _send_error(
        self,
        sender: FrameSender,
        *,
        request_id: RequestId | None,
        code: ConnectionErrorCode,
    ) -> None:
        response = CommandErrorReply(request_id=request_id, error_code=code)
        await self._send(sender, response.model_dump_json().encode())

    async def _fail_fatal(
        self,
        sender: FrameSender,
        request_id: RequestId,
        cause: Exception,
    ) -> Never:
        try:
            await self._send_error(
                sender,
                request_id=request_id,
                code=ConnectionErrorCode.INTERNAL_ERROR,
            )
        except Exception as send_error:
            raise ConnectionFatalError(
                "connection error response failed"
            ) from send_error
        raise ConnectionFatalError("connection dispatcher failed") from cause

    async def _send(self, sender: FrameSender, payload: bytes) -> None:
        frame = encode_frame(
            payload,
            maximum_frame_bytes=self._maximum_frame_bytes,
        )
        await sender(frame)

    def _require_open(self) -> None:
        if self._closed:
            raise ConnectionClosedError("connection closed")

    def _abort(self) -> None:
        self._closed = True
        self._cancellation_event.set()
        self._decoder.reset()

    @staticmethod
    async def _cancel_and_wait(task: asyncio.Task[object]) -> None:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
