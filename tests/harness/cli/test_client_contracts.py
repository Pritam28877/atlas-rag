"""CLI validation, response, and transport-bound tests."""

import asyncio
import json
from typing import Final

import pytest

from app.cli.harness.client_contracts import (
    CliCommandError,
    CliExitCode,
    HarnessCliClient,
    build_command_envelope,
    parse_command_response,
    response_exit_code,
)
from app.services.harness.protocol import (
    CommandKind,
)
from app.services.harness.runtime import CommandErrorReply, ConnectionErrorCode

WORKSPACE: Final = "wsp_" + "1" * 32
CLIENT: Final = "cli_" + "2" * 32
REQUEST: Final = "req_" + "3" * 32


class RecordingTransport:
    def __init__(self, response: bytes, *, delay_seconds: float = 0) -> None:
        self.response = response
        self.delay_seconds = delay_seconds
        self.payloads: list[bytes] = []

    async def request(self, payload: bytes, timeout_seconds: float) -> bytes:
        self.payloads.append(payload)
        await asyncio.sleep(self.delay_seconds)
        return self.response


def _envelope() -> object:
    return build_command_envelope(
        command_payload={
            "kind": "thread.list",
            "page": {"limit": 1},
            "states": [],
        },
        workspace_id=WORKSPACE,
        client_id=CLIENT,
        request_id=REQUEST,
    )


def test_all_registered_kinds_are_exposed_by_validation_registry() -> None:
    registered = {kind.value for kind in CommandKind}
    assert "workspace.open" in registered
    assert "evaluation.status" in registered
    assert len(registered) == 21


def test_validation_rejects_missing_fields_and_oversized_payload() -> None:
    with pytest.raises(CliCommandError, match="versioned protocol"):
        build_command_envelope(
            command_payload={"kind": "thread.create"},
            workspace_id=WORKSPACE,
            client_id=CLIENT,
            request_id=REQUEST,
        )
    with pytest.raises(CliCommandError, match="payload exceeds"):
        build_command_envelope(
            command_payload={"kind": "thread.list", "padding": "x" * 1_100_000},
            workspace_id=WORKSPACE,
            client_id=CLIENT,
            request_id=REQUEST,
        )


def test_response_decoding_preserves_server_error_and_exit_code() -> None:
    response = parse_command_response(
        CommandErrorReply(
            request_id=REQUEST,
            error_code=ConnectionErrorCode.AUTHORITY_DENIED,
        ).model_dump_json().encode()
    )
    assert isinstance(response, CommandErrorReply)
    assert response_exit_code(response) is CliExitCode.AUTHENTICATION


def test_client_transport_timeout_is_bounded() -> None:
    transport = RecordingTransport(b"{}", delay_seconds=0.05)
    with pytest.raises(CliCommandError, match="timed out") as error_info:
        asyncio.run(
            HarnessCliClient(transport).execute(
                _envelope(),
                timeout_seconds=0.001,
            )
        )
    assert error_info.value.exit_code is CliExitCode.TIMEOUT


def test_cli_response_payload_is_valid_json() -> None:
    payload = {
        "status": "error",
        "request_id": REQUEST,
        "error_code": "internal_error",
    }
    response = parse_command_response(json.dumps(payload).encode())
    assert response.status == "error"
