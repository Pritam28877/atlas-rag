"""Bounded MCP host lifecycle and untrusted response tests."""

import asyncio
import hashlib
from collections.abc import Mapping

import pytest

from app.services.harness.extensions import (
    McpCallRequest,
    McpEgressPolicy,
    McpHost,
    McpHostError,
    McpHostErrorCode,
    McpServerDescriptor,
    McpTransportKind,
)


def _request(request_id: str = "req_" + "1" * 32) -> McpCallRequest:
    return McpCallRequest(
        request_id=request_id,
        server_name="weather",
        method="tools/list",
        params_json="{}",
        capability="mcp.invoke",
        timeout_ms=100,
    )


def _descriptor(*, max_pending_calls: int = 32) -> McpServerDescriptor:
    return McpServerDescriptor(
        name="weather",
        version="1.0.0",
        transport=McpTransportKind.STDIO,
        credential_handles=(),
        max_pending_calls=max_pending_calls,
    )


class FakeTransport:
    def __init__(self, response: object, *, delay: float = 0.0) -> None:
        self.response = response
        self.delay = delay
        self.calls = 0
        self.closed = False

    async def call(
        self,
        request: McpCallRequest,
    ) -> bytes | str | Mapping[str, object]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.response, (bytes, str, Mapping)):
            return self.response
        raise RuntimeError("invalid fake response")

    async def close(self) -> None:
        self.closed = True


def _success(request_id: str) -> dict[str, object]:
    return {"request_id": request_id, "result_json": '{"ok":true}'}


def test_mcp_host_normalizes_response_and_closes_transport() -> None:
    async def scenario() -> None:
        transport = FakeTransport(_success(_request().request_id))
        host = McpHost(_descriptor(), transport)
        result = await host.call(_request())

        assert result.result_json == '{"ok":true}'
        assert result.response_sha256 == hashlib.sha256(
            b'{"ok":true}'
        ).hexdigest()
        await host.close()
        assert transport.closed
        assert host.closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("response", "expected"),
    (
        (
            {"request_id": "req_" + "1" * 32, "result_json": "not-json"},
            "malformed_response",
        ),
        (
            {"request_id": "req_" + "2" * 32, "result_json": "{}"},
            "malformed_response",
        ),
    ),
)
def test_mcp_host_rejects_malformed_or_mismatched_response(
    response: dict[str, object], expected: str
) -> None:
    async def scenario() -> None:
        host = McpHost(_descriptor(), FakeTransport(response))
        with pytest.raises(McpHostError) as failure:
            await host.call(_request())
        assert failure.value.code.value == expected
        await host.close()

    asyncio.run(scenario())


def test_mcp_host_enforces_pending_limit_and_cleans_on_close() -> None:
    async def scenario() -> None:
        transport = FakeTransport(_success(_request().request_id), delay=1)
        host = McpHost(_descriptor(max_pending_calls=1), transport)
        first = asyncio.create_task(host.call(_request()))
        await asyncio.sleep(0)
        with pytest.raises(McpHostError) as failure:
            await host.call(_request("req_" + "2" * 32))
        assert failure.value.code is McpHostErrorCode.PENDING_LIMIT
        await host.close()
        await asyncio.gather(first, return_exceptions=True)
        assert host.pending_count == 0
        assert transport.closed

    asyncio.run(scenario())


def test_mcp_host_timeout_and_explicit_cancellation_are_bounded() -> None:
    async def scenario() -> None:
        timeout_host = McpHost(_descriptor(), FakeTransport({}, delay=1))
        with pytest.raises(McpHostError) as timeout:
            await timeout_host.call(_request())
        assert timeout.value.code is McpHostErrorCode.TIMEOUT
        await timeout_host.close()

        cancellation = asyncio.Event()
        cancel_host = McpHost(
            _descriptor(), FakeTransport({}, delay=1)
        )
        pending = asyncio.create_task(
            cancel_host.call(_request(), cancellation=cancellation)
        )
        await asyncio.sleep(0)
        cancellation.set()
        with pytest.raises(McpHostError) as cancelled:
            await pending
        assert cancelled.value.code is McpHostErrorCode.CANCELLED
        await cancel_host.close()

    asyncio.run(scenario())


def test_http_mcp_requires_tls_and_allowlisted_host() -> None:
    with pytest.raises(ValueError):
        McpServerDescriptor(
            name="weather",
            version="1.0.0",
            transport=McpTransportKind.HTTP,
            endpoint="http://example.test/mcp",
            egress=McpEgressPolicy(allowed_hosts=("example.test",)),
            credential_handles=(),
        )
