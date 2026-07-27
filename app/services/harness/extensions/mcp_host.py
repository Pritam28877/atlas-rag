"""Bounded lifecycle host for untrusted MCP transports."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol

from app.services.harness.extensions.mcp_contracts import (
    McpCallRequest,
    McpCallResult,
    McpServerDescriptor,
    parse_mcp_response,
)


class McpTransport(Protocol):
    async def call(
        self,
        request: McpCallRequest,
    ) -> bytes | str | Mapping[str, object]: ...

    async def close(self) -> None: ...


class McpHostErrorCode(StrEnum):
    CLOSED = "closed"
    DUPLICATE_REQUEST = "duplicate_request"
    INVALID_REQUEST = "invalid_request"
    PENDING_LIMIT = "pending_limit"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    MALFORMED_RESPONSE = "malformed_response"
    TRANSPORT = "transport"


class McpHostError(RuntimeError):
    def __init__(self, code: McpHostErrorCode) -> None:
        super().__init__("MCP host rejected or failed the request")
        self.code = code


class McpHost:
    """Own one per-workspace MCP transport and all of its pending calls."""

    def __init__(
        self,
        descriptor: McpServerDescriptor,
        transport: McpTransport,
    ) -> None:
        self._descriptor = McpServerDescriptor.model_validate(
            descriptor.model_dump()
        )
        self._transport = transport
        self._pending: dict[str, asyncio.Task[McpCallResult]] = {}
        self._closed = False

    @property
    def descriptor(self) -> McpServerDescriptor:
        return self._descriptor

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def closed(self) -> bool:
        return self._closed

    async def call(
        self,
        request: McpCallRequest,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> McpCallResult:
        verified_request = McpCallRequest.model_validate(request.model_dump())
        if self._closed:
            raise McpHostError(McpHostErrorCode.CLOSED)
        if verified_request.server_name != self._descriptor.name:
            raise McpHostError(McpHostErrorCode.INVALID_REQUEST)
        if verified_request.request_id in self._pending:
            raise McpHostError(McpHostErrorCode.DUPLICATE_REQUEST)
        if len(self._pending) >= self._descriptor.max_pending_calls:
            raise McpHostError(McpHostErrorCode.PENDING_LIMIT)
        task = asyncio.create_task(self._execute(verified_request, cancellation))
        self._pending[verified_request.request_id] = task
        try:
            return await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise McpHostError(McpHostErrorCode.CANCELLED) from None
        finally:
            self._pending.pop(verified_request.request_id, None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pending = tuple(self._pending.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            await self._transport.close()
        except Exception:
            raise McpHostError(McpHostErrorCode.TRANSPORT) from None
        finally:
            self._pending.clear()

    async def _execute(
        self,
        request: McpCallRequest,
        cancellation: asyncio.Event | None,
    ) -> McpCallResult:
        transport_task = asyncio.create_task(self._transport.call(request))
        cancellation_task = (
            asyncio.create_task(cancellation.wait())
            if cancellation is not None
            else None
        )
        wait_set: set[asyncio.Task[object]] = {transport_task}
        if cancellation_task is not None:
            wait_set.add(cancellation_task)
        try:
            done, _ = await asyncio.wait(
                wait_set,
                timeout=request.timeout_ms / 1000,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                transport_task.cancel()
                await asyncio.gather(transport_task, return_exceptions=True)
                raise McpHostError(McpHostErrorCode.TIMEOUT)
            if cancellation_task is not None and cancellation_task in done:
                transport_task.cancel()
                await asyncio.gather(transport_task, return_exceptions=True)
                raise McpHostError(McpHostErrorCode.CANCELLED)
            try:
                raw_response = await transport_task
            except McpHostError:
                raise
            except asyncio.CancelledError:
                raise McpHostError(McpHostErrorCode.CANCELLED) from None
            except Exception:
                raise McpHostError(McpHostErrorCode.TRANSPORT) from None
            try:
                return parse_mcp_response(
                    request,
                    raw_response,
                    maximum_bytes=self._descriptor.max_response_bytes,
                )
            except Exception:
                raise McpHostError(McpHostErrorCode.MALFORMED_RESPONSE) from None
        except asyncio.CancelledError:
            transport_task.cancel()
            await asyncio.gather(transport_task, return_exceptions=True)
            raise
        finally:
            if cancellation_task is not None:
                cancellation_task.cancel()
                await asyncio.gather(cancellation_task, return_exceptions=True)
