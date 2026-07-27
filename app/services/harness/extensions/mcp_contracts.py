"""Bounded wire contracts for untrusted Model Context Protocol servers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Self
from urllib.parse import urlparse

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedLabel,
    BoundedReason,
    Capability,
    RequestId,
    Sha256,
    StrictProtocolModel,
)

MCP_DEFAULT_PENDING_CALLS = 32
MCP_HARD_PENDING_CALLS = 256
MCP_MAX_PARAMETER_BYTES = 128 * 1024
MCP_MAX_RESPONSE_BYTES = 16 * 1024 * 1024

McpMethod = BoundedLabel
McpCredentialHandle = str
McpServerName = str
McpVersion = str


class McpTransportKind(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


class McpEgressPolicy(StrictProtocolModel):
    """Destination constraints for a remote MCP server."""

    allowed_hosts: tuple[BoundedLabel, ...] = Field(max_length=64)
    require_tls: bool = True
    max_redirects: int = Field(default=0, ge=0, le=5)
    allow_private_networks: bool = False

    @model_validator(mode="after")
    def validate_hosts(self) -> Self:
        if tuple(sorted(set(self.allowed_hosts))) != self.allowed_hosts:
            raise ValueError("MCP egress hosts must be unique and sorted")
        if self.allow_private_networks:
            raise ValueError("private MCP egress requires a reviewed policy")
        return self


class McpServerDescriptor(StrictProtocolModel):
    """Identity and resource policy for one isolated MCP server."""

    name: McpServerName = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9._-]*$",
    )
    version: McpVersion = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$",
    )
    transport: McpTransportKind
    endpoint: str | None = Field(default=None, max_length=2048)
    egress: McpEgressPolicy | None = None
    credential_handles: tuple[McpCredentialHandle, ...] = Field(max_length=16)
    max_pending_calls: int = Field(
        default=MCP_DEFAULT_PENDING_CALLS,
        ge=1,
        le=MCP_HARD_PENDING_CALLS,
    )
    max_response_bytes: int = Field(
        default=1024 * 1024,
        ge=1,
        le=MCP_MAX_RESPONSE_BYTES,
    )

    @model_validator(mode="after")
    def validate_transport(self) -> Self:
        if tuple(sorted(set(self.credential_handles))) != self.credential_handles:
            raise ValueError("MCP credential handles must be unique and sorted")
        if self.transport is McpTransportKind.STDIO:
            if self.endpoint is not None or self.egress is not None:
                raise ValueError("stdio MCP servers cannot have an HTTP endpoint")
            return self
        if self.endpoint is None or self.egress is None:
            raise ValueError("HTTP MCP servers require endpoint and egress policy")
        parsed = urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MCP endpoint must be an absolute HTTP URL")
        if self.egress.require_tls and parsed.scheme != "https":
            raise ValueError("MCP HTTP endpoint must use TLS")
        if parsed.hostname not in self.egress.allowed_hosts:
            raise ValueError("MCP endpoint host is not allowlisted")
        if parsed.username or parsed.password:
            raise ValueError("MCP endpoint must not contain credentials")
        return self


class McpCallRequest(StrictProtocolModel):
    """Canonical request sent to an isolated MCP transport."""

    request_id: RequestId
    server_name: McpServerName = Field(min_length=1, max_length=128)
    method: McpMethod
    params_json: str = Field(min_length=2, max_length=MCP_MAX_PARAMETER_BYTES)
    capability: Capability
    timeout_ms: int = Field(default=60_000, ge=100, le=3_600_000)

    @model_validator(mode="after")
    def validate_params(self) -> Self:
        if len(self.params_json.encode("utf-8")) > MCP_MAX_PARAMETER_BYTES:
            raise ValueError("MCP parameters exceed byte limit")
        try:
            parsed = json.loads(self.params_json)
            canonical = json.dumps(
                parsed,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("MCP parameters must be valid JSON") from error
        if canonical != self.params_json:
            raise ValueError("MCP parameters must use canonical JSON")
        return self


class McpResponseEnvelope(StrictProtocolModel):
    """Validated response envelope produced by an untrusted transport."""

    request_id: RequestId
    result_json: str | None = Field(default=None, max_length=MCP_MAX_RESPONSE_BYTES)
    error_code: BoundedLabel | None = None
    error_message: BoundedReason | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        has_result = self.result_json is not None
        has_error = self.error_code is not None or self.error_message is not None
        if has_result == has_error:
            raise ValueError("MCP response must contain exactly one result or error")
        if has_error and (self.error_code is None or self.error_message is None):
            raise ValueError("MCP errors require code and message")
        if self.result_json is not None:
            try:
                parsed = json.loads(self.result_json)
                canonical = json.dumps(
                    parsed,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("MCP result must be valid JSON") from error
            if canonical != self.result_json:
                raise ValueError("MCP result must use canonical JSON")
        return self


class McpCallResult(StrictProtocolModel):
    """Model-visible result with a stable digest and bounded payload."""

    request_id: RequestId
    result_json: str | None = Field(default=None, max_length=MCP_MAX_RESPONSE_BYTES)
    error_code: BoundedLabel | None = None
    error_message: BoundedReason | None = None
    response_bytes: int = Field(ge=1, le=MCP_MAX_RESPONSE_BYTES)
    response_sha256: Sha256

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        serialized = self.result_json or f"{self.error_code}:{self.error_message}"
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        if digest != self.response_sha256:
            raise ValueError("MCP response digest is invalid")
        if len(serialized.encode("utf-8")) != self.response_bytes:
            raise ValueError("MCP response size is invalid")
        return self


def parse_mcp_response(
    request: McpCallRequest,
    raw_response: bytes | str | Mapping[str, object],
    *,
    maximum_bytes: int,
) -> McpCallResult:
    """Parse and normalize an untrusted transport response."""

    if isinstance(raw_response, bytes):
        if len(raw_response) > maximum_bytes:
            raise ValueError("MCP response exceeds byte limit")
        try:
            decoded = raw_response.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("MCP response is not UTF-8") from error
    elif isinstance(raw_response, str):
        decoded = raw_response
        if len(decoded.encode("utf-8")) > maximum_bytes:
            raise ValueError("MCP response exceeds byte limit")
    else:
        decoded = json.dumps(
            dict(raw_response),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(decoded.encode("utf-8")) > maximum_bytes:
            raise ValueError("MCP response exceeds byte limit")
    try:
        parsed = json.loads(decoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("MCP response is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("MCP response must be an object")
    envelope = McpResponseEnvelope.model_validate(parsed)
    if envelope.request_id != request.request_id:
        raise ValueError("MCP response request id does not match")
    serialized = envelope.result_json or (
        f"{envelope.error_code}:{envelope.error_message}"
    )
    return McpCallResult(
        request_id=envelope.request_id,
        result_json=envelope.result_json,
        error_code=envelope.error_code,
        error_message=envelope.error_message,
        response_bytes=len(serialized.encode("utf-8")),
        response_sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    )
