"""Secret-safe command-line entrypoint for the versioned Atlas Harness protocol."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from typing import cast

from app.cli.harness.client_contracts import (
    CliCommandError,
    CliExitCode,
    HarnessCliClient,
    build_command_envelope,
    response_exit_code,
    response_metadata,
    validation_receipt,
)
from app.services.harness.protocol import (
    ClientId,
    CommandEnvelope,
    CommandKind,
    RequestId,
    WorkspaceId,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command != "validate":
        parser.error("only the versioned validate command is available offline")
    try:
        command_payload = _load_payload(arguments.payload)
        envelope = build_command_envelope(
            command_payload=command_payload,
            workspace_id=cast(WorkspaceId, arguments.workspace_id),
            client_id=cast(ClientId, arguments.client_id),
            request_id=cast(RequestId, arguments.request_id),
            expected_sequence=arguments.expected_sequence,
        )
        print(validation_receipt(envelope).model_dump_json())
        return int(CliExitCode.SUCCESS)
    except CliCommandError as error:
        _write_error(error)
        return int(error.exit_code)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _write_error_and_code(
            CliCommandError(
                "invalid_input",
                "CLI arguments are invalid",
                CliExitCode.INVALID_INPUT,
            )
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atlas-harness")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--workspace-id", required=True)
    validate.add_argument("--client-id", required=True)
    validate.add_argument("--request-id", required=True)
    validate.add_argument("--payload", required=True)
    validate.add_argument("--expected-sequence", type=int)
    return parser


def _load_payload(raw_payload: str) -> Mapping[str, object]:
    parsed = json.loads(raw_payload)
    if not isinstance(parsed, dict):
        raise TypeError("command payload must be a JSON object")
    command_kind = parsed.get("kind")
    if not isinstance(command_kind, str) or command_kind not in {
        kind.value for kind in CommandKind
    }:
        raise ValueError("command kind is not registered")
    return parsed


def _write_error(error: CliCommandError) -> None:
    print(
        json.dumps(
            {"status": "error", "code": error.code, "message": str(error)},
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        file=sys.stderr,
    )


def _write_error_and_code(error: CliCommandError) -> int:
    _write_error(error)
    return int(error.exit_code)


async def execute_with_client(
    client: HarnessCliClient,
    envelope: CommandEnvelope,
    *,
    timeout_seconds: float,
) -> int:
    """Small async adapter used by the daemon-backed CLI runner."""

    response = await client.execute(
        envelope,
        timeout_seconds=timeout_seconds,
    )
    print(json.dumps(response_metadata(response), ensure_ascii=True, default=str))
    return int(response_exit_code(response))


if __name__ == "__main__":
    raise SystemExit(main())
