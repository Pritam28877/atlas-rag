"""Typed, bounded local-client contracts shared by CLI and future TUI clients."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from enum import IntEnum
from typing import Protocol

from pydantic import ValidationError

from app.services.harness.protocol import (
    CURRENT_SCHEMA_VERSION,
    ClientId,
    CommandEnvelope,
    CommandKind,
    RequestId,
    SchemaVersion,
    StrictProtocolModel,
    WorkspaceId,
    command_request_sha256,
)
from app.services.harness.runtime import (
    CommandErrorReply,
    CommandReply,
)

MAXIMUM_CLI_PAYLOAD_BYTES = 1024 * 1024
MINIMUM_COMMAND_TIMEOUT_SECONDS = 0.001
MAXIMUM_COMMAND_TIMEOUT_SECONDS = 3_600.0


class CliExitCode(IntEnum):
    SUCCESS = 0
    INVALID_INPUT = 2
    AUTHENTICATION = 3
    TIMEOUT = 4
    TRANSPORT = 5
    SERVER = 6
    RESYNC_REQUIRED = 7


class CliCommandError(ValueError):
    """Input or transport failure with a stable, secret-safe code."""

    def __init__(self, code: str, message: str, exit_code: CliExitCode) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class CliValidationReceipt(StrictProtocolModel):
    """Metadata-only validation output; command arguments are never echoed."""

    status: str = "validated"
    schema_version: SchemaVersion
    command_kind: CommandKind
    workspace_id: WorkspaceId
    request_id: RequestId
    request_sha256: str


class CommandTransport(Protocol):
    async def request(self, payload: bytes, timeout_seconds: float) -> bytes: ...


def build_command_envelope(
    *,
    command_payload: Mapping[str, object],
    workspace_id: WorkspaceId,
    client_id: ClientId,
    request_id: RequestId,
    schema_version: SchemaVersion = CURRENT_SCHEMA_VERSION,
    expected_sequence: int | None = None,
) -> CommandEnvelope:
    """Validate one CLI command against the canonical protocol registry."""

    payload_bytes = json.dumps(
        command_payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload_bytes) > MAXIMUM_CLI_PAYLOAD_BYTES:
        raise CliCommandError(
            "payload_too_large",
            "command payload exceeds the CLI limit",
            CliExitCode.INVALID_INPUT,
        )
    try:
        envelope_data = {
            "schema_version": schema_version,
            "request_id": request_id,
            "client_id": client_id,
            "workspace_id": workspace_id,
            "expected_sequence": expected_sequence,
            "command": dict(command_payload),
        }
        return CommandEnvelope.model_validate_json(
            json.dumps(
                envelope_data,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    except ValidationError as error:
        raise CliCommandError(
            "invalid_command",
            "command does not satisfy the versioned protocol",
            CliExitCode.INVALID_INPUT,
        ) from error


def validation_receipt(envelope: CommandEnvelope) -> CliValidationReceipt:
    """Return only non-secret command metadata for dry-run output."""

    return CliValidationReceipt(
        schema_version=envelope.schema_version,
        command_kind=CommandKind(envelope.command.kind),
        workspace_id=envelope.workspace_id,
        request_id=envelope.request_id,
        request_sha256=command_request_sha256(envelope),
    )


class HarnessCliClient:
    """Executes validated commands through an authenticated transport."""

    def __init__(self, transport: CommandTransport) -> None:
        self._transport = transport

    async def execute(
        self,
        envelope: CommandEnvelope,
        *,
        timeout_seconds: float,
    ) -> CommandReply | CommandErrorReply:
        if not (
            MINIMUM_COMMAND_TIMEOUT_SECONDS
            <= timeout_seconds
            <= MAXIMUM_COMMAND_TIMEOUT_SECONDS
        ):
            raise CliCommandError(
                "invalid_timeout",
                "command timeout is outside the configured bounds",
                CliExitCode.INVALID_INPUT,
            )
        payload = envelope.model_dump_json().encode("utf-8")
        try:
            raw_response = await asyncio.wait_for(
                self._transport.request(payload, timeout_seconds),
                timeout=timeout_seconds,
            )
        except TimeoutError as error:
            raise CliCommandError(
                "timeout",
                "command transport timed out",
                CliExitCode.TIMEOUT,
            ) from error
        except OSError as error:
            raise CliCommandError(
                "transport_error",
                "command transport failed",
                CliExitCode.TRANSPORT,
            ) from error
        return parse_command_response(raw_response)


def parse_command_response(raw_response: bytes) -> CommandReply | CommandErrorReply:
    """Decode only the two server response envelopes; reject unknown statuses."""

    try:
        response_data = json.loads(raw_response)
        if not isinstance(response_data, dict):
            raise ValueError("response is not an object")
        response_status = response_data.get("status")
        if response_status == "success":
            return CommandReply.model_validate_json(raw_response)
        if response_status == "error":
            return CommandErrorReply.model_validate_json(raw_response)
    except (TypeError, ValueError, ValidationError) as error:
        raise CliCommandError(
            "invalid_response",
            "daemon returned an invalid response envelope",
            CliExitCode.SERVER,
        ) from error
    raise CliCommandError(
        "invalid_response",
        "daemon returned an unknown response status",
        CliExitCode.SERVER,
    )


def response_exit_code(response: CommandReply | CommandErrorReply) -> CliExitCode:
    """Map a typed response to a stable process exit status."""

    if isinstance(response, CommandReply):
        return CliExitCode.SUCCESS
    if response.error_code.value == "authority_denied":
        return CliExitCode.AUTHENTICATION
    if response.error_code.value == "resync_required":
        return CliExitCode.RESYNC_REQUIRED
    return CliExitCode.SERVER


def response_metadata(
    response: CommandReply | CommandErrorReply,
) -> dict[str, object]:
    """Serialize a response without inventing a second protocol shape."""

    if isinstance(response, CommandReply):
        return {
            "status": response.status,
            "request_id": response.request_id,
            "response_kind": response.response_kind,
            "payload": response.payload.model_dump(mode="json"),
        }
    return {
        "status": response.status,
        "request_id": response.request_id,
        "error_code": response.error_code,
    }
