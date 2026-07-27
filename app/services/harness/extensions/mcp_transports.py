"""Policy-bound RPC adapters for isolated MCP channels."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol

from app.services.harness.extensions.mcp_contracts import (
    McpCallRequest,
    McpEgressPolicy,
    McpInitializeRequest,
    McpNegotiatedSession,
    McpServerDescriptor,
    McpTransportKind,
)


class McpStdioChannel(Protocol):
    """Channel owned by the sandbox supervisor, never by the trusted host."""

    async def exchange(
        self,
        payload: bytes,
        *,
        cancellation: object | None = None,
    ) -> bytes: ...

    async def close(self) -> None: ...


class McpHttpChannel(Protocol):
    """Egress gateway that owns TLS, redirects, and credential injection."""

    async def post(
        self,
        endpoint: str,
        payload: bytes,
        *,
        egress: McpEgressPolicy,
        cancellation: object | None = None,
    ) -> bytes: ...

    async def close(self) -> None: ...


class McpStdioTransport:
    """Translate canonical calls to a sandbox-owned JSON-RPC channel."""

    def __init__(
        self,
        descriptor: McpServerDescriptor,
        channel: McpStdioChannel,
    ) -> None:
        if descriptor.transport is not McpTransportKind.STDIO:
            raise ValueError("stdio transport requires a stdio descriptor")
        self._descriptor = descriptor
        self._channel = channel

    async def call(
        self,
        request: McpCallRequest,
    ) -> bytes | str | Mapping[str, object]:
        payload = _encode_call(request)
        raw_response = await self._channel.exchange(payload)
        return _normalize_rpc_response(raw_response, request)

    async def initialize(
        self,
        request: McpInitializeRequest,
    ) -> McpNegotiatedSession:
        raw_response = await self._channel.exchange(_encode_initialize(request))
        return _parse_initialize_response(raw_response, request)

    async def close(self) -> None:
        await self._channel.close()


class McpHttpTransport:
    """Translate canonical calls through a policy-enforcing HTTP gateway."""

    def __init__(
        self,
        descriptor: McpServerDescriptor,
        channel: McpHttpChannel,
    ) -> None:
        if descriptor.transport is not McpTransportKind.HTTP:
            raise ValueError("HTTP transport requires an HTTP descriptor")
        if descriptor.endpoint is None or descriptor.egress is None:
            raise ValueError("HTTP descriptor lacks endpoint policy")
        self._descriptor = descriptor
        self._channel = channel

    async def call(
        self,
        request: McpCallRequest,
    ) -> bytes | str | Mapping[str, object]:
        raw_response = await self._channel.post(
            self._endpoint,
            _encode_call(request),
            egress=self._egress,
        )
        return _normalize_rpc_response(raw_response, request)

    async def initialize(
        self,
        request: McpInitializeRequest,
    ) -> McpNegotiatedSession:
        raw_response = await self._channel.post(
            self._endpoint,
            _encode_initialize(request),
            egress=self._egress,
        )
        return _parse_initialize_response(raw_response, request)

    async def close(self) -> None:
        await self._channel.close()

    @property
    def _endpoint(self) -> str:
        if self._descriptor.endpoint is None:
            raise RuntimeError("HTTP endpoint is missing")
        return self._descriptor.endpoint

    @property
    def _egress(self) -> McpEgressPolicy:
        if self._descriptor.egress is None:
            raise RuntimeError("HTTP egress policy is missing")
        return self._descriptor.egress


def _encode_call(request: McpCallRequest) -> bytes:
    params = json.loads(request.params_json)
    return _encode_rpc(
        {
            "jsonrpc": "2.0",
            "id": request.request_id,
            "method": request.method,
            "params": params,
        }
    )


def _encode_initialize(request: McpInitializeRequest) -> bytes:
    capabilities = json.loads(request.capabilities_json)
    return _encode_rpc(
        {
            "jsonrpc": "2.0",
            "id": "initialize",
            "method": "initialize",
            "params": {
                "protocolVersion": request.protocol_version,
                "clientInfo": {
                    "name": request.client_name,
                    "version": request.client_version,
                },
                "capabilities": capabilities,
            },
        }
    )


def _encode_rpc(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _normalize_rpc_response(
    raw_response: bytes,
    request: McpCallRequest,
) -> Mapping[str, object]:
    payload = _decode_rpc(raw_response)
    if payload.get("id") != request.request_id:
        raise ValueError("MCP JSON-RPC response id does not match")
    if "result" in payload and "error" not in payload:
        return {
            "request_id": request.request_id,
            "result_json": _canonical_json(payload["result"]),
        }
    if "error" not in payload or "result" in payload:
        raise ValueError("MCP JSON-RPC response must contain result or error")
    error = payload["error"]
    if not isinstance(error, dict):
        raise ValueError("MCP JSON-RPC error must be an object")
    code = error.get("code")
    message = error.get("message")
    if not isinstance(code, int) or not isinstance(message, str):
        raise ValueError("MCP JSON-RPC error fields are invalid")
    return {
        "request_id": request.request_id,
        "error_code": f"mcp.error.{code}",
        "error_message": message,
    }


def _parse_initialize_response(
    raw_response: bytes,
    request: McpInitializeRequest,
) -> McpNegotiatedSession:
    payload = _decode_rpc(raw_response)
    if payload.get("id") != "initialize" or "result" not in payload:
        raise ValueError("MCP initialize response is invalid")
    result = payload["result"]
    if not isinstance(result, dict):
        raise ValueError("MCP initialize result must be an object")
    protocol_version = result.get("protocolVersion")
    server_info = result.get("serverInfo")
    capabilities = result.get("capabilities")
    if (
        not isinstance(protocol_version, str)
        or not isinstance(server_info, dict)
        or not isinstance(capabilities, dict)
    ):
        raise ValueError("MCP initialize result fields are invalid")
    server_name = server_info.get("name")
    server_version = server_info.get("version")
    if not isinstance(server_name, str) or not isinstance(server_version, str):
        raise ValueError("MCP server info is invalid")
    if protocol_version != request.protocol_version:
        raise ValueError("MCP protocol version negotiation failed")
    return McpNegotiatedSession(
        protocol_version=protocol_version,
        server_name=server_name,
        server_version=server_version,
        capabilities_json=_canonical_json(capabilities),
    )


def _decode_rpc(raw_response: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw_response.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("MCP JSON-RPC response is invalid") from error
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise ValueError("MCP JSON-RPC response envelope is invalid")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
