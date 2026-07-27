"""Bounded host for out-of-process plugin channels."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol

from app.services.harness.extensions.plugin_contracts import (
    PluginCall,
    PluginDescriptor,
    PluginResult,
)


class PluginTransport(Protocol):
    async def invoke(
        self,
        call: PluginCall,
    ) -> bytes | str | Mapping[str, object]: ...

    async def close(self) -> None: ...


class PluginHostErrorCode(StrEnum):
    CLOSED = "closed"
    PENDING_LIMIT = "pending_limit"
    DUPLICATE_REQUEST = "duplicate_request"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    MALFORMED_RESULT = "malformed_result"
    TRANSPORT = "transport"
    CAPABILITY_DENIED = "capability_denied"


class PluginHostError(RuntimeError):
    def __init__(self, code: PluginHostErrorCode) -> None:
        super().__init__("plugin host rejected or failed the request")
        self.code = code


class PluginHost:
    def __init__(
        self,
        descriptor: PluginDescriptor,
        transport: PluginTransport,
    ) -> None:
        self._descriptor = PluginDescriptor.model_validate(descriptor.model_dump())
        self._transport = transport
        self._pending: dict[str, asyncio.Task[PluginResult]] = {}
        self._closed = False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def call(
        self,
        call: PluginCall,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> PluginResult:
        verified = PluginCall.model_validate(call.model_dump())
        if self._closed:
            raise PluginHostError(PluginHostErrorCode.CLOSED)
        if verified.capability not in self._descriptor.capabilities:
            raise PluginHostError(PluginHostErrorCode.CAPABILITY_DENIED)
        if verified.request_id in self._pending:
            raise PluginHostError(PluginHostErrorCode.DUPLICATE_REQUEST)
        if len(self._pending) >= self._descriptor.max_pending_calls:
            raise PluginHostError(PluginHostErrorCode.PENDING_LIMIT)
        task = asyncio.create_task(self._execute(verified, cancellation))
        self._pending[verified.request_id] = task
        try:
            return await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise PluginHostError(PluginHostErrorCode.CANCELLED) from None
        finally:
            self._pending.pop(verified.request_id, None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._pending.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self._transport.close()
        except Exception:
            raise PluginHostError(PluginHostErrorCode.TRANSPORT) from None
        finally:
            self._pending.clear()

    async def _execute(
        self,
        call: PluginCall,
        cancellation: asyncio.Event | None,
    ) -> PluginResult:
        invocation = asyncio.create_task(self._transport.invoke(call))
        cancellation_task = (
            asyncio.create_task(cancellation.wait())
            if cancellation is not None
            else None
        )
        wait_set: set[asyncio.Task[object]] = {invocation}
        if cancellation_task is not None:
            wait_set.add(cancellation_task)
        try:
            done, _ = await asyncio.wait(
                wait_set,
                timeout=call.timeout_ms / 1000,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)
                raise PluginHostError(PluginHostErrorCode.TIMEOUT)
            if cancellation_task is not None and cancellation_task in done:
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)
                raise PluginHostError(PluginHostErrorCode.CANCELLED)
            raw = await invocation
            try:
                if isinstance(raw, bytes):
                    parsed = json.loads(raw.decode("utf-8"))
                elif isinstance(raw, str):
                    parsed = json.loads(raw)
                else:
                    parsed = dict(raw)
                result = PluginResult.model_validate(parsed)
            except Exception:
                raise PluginHostError(PluginHostErrorCode.MALFORMED_RESULT) from None
            if result.request_id != call.request_id:
                raise PluginHostError(PluginHostErrorCode.MALFORMED_RESULT)
            return result
        except asyncio.CancelledError:
            invocation.cancel()
            await asyncio.gather(invocation, return_exceptions=True)
            raise
        except PluginHostError:
            raise
        except Exception:
            raise PluginHostError(PluginHostErrorCode.TRANSPORT) from None
        finally:
            if cancellation_task is not None:
                cancellation_task.cancel()
                await asyncio.gather(cancellation_task, return_exceptions=True)
