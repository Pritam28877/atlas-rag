"""Deterministic compatibility fixture documents for protocol generation."""

from pathlib import Path

from app.services.harness.protocol import CURRENT_SCHEMA_VERSION

FIXTURE_ROOT = Path("tests/fixtures/harness/protocol")
READER_V1_0_PATH = FIXTURE_ROOT / "reader-v1.0-command.json"
READER_V1_1_PATH = FIXTURE_ROOT / "reader-v1.1-command.json"
READER_V1_2_PATH = FIXTURE_ROOT / "reader-v1.2-command.json"
ROLLBACK_V1_0_PATH = FIXTURE_ROOT / "rollback-v1.0-unknown-event.json"
ROLLBACK_V1_1_PATH = FIXTURE_ROOT / "rollback-v1.1-unknown-command.json"
FIXTURE_PATHS = (
    READER_V1_0_PATH,
    READER_V1_1_PATH,
    READER_V1_2_PATH,
    ROLLBACK_V1_0_PATH,
    ROLLBACK_V1_1_PATH,
)


def _command_envelope(
    schema_version: str,
    command: dict[str, object],
) -> dict[str, object]:
    return {
        "client_id": "cli_0123456789abcdef0123456789abcdef",
        "command": command,
        "request_id": "req_0123456789abcdef0123456789abcdef",
        "schema_version": schema_version,
        "workspace_id": "wsp_0123456789abcdef0123456789abcdef",
    }


def build_compatibility_fixture_documents() -> dict[Path, object]:
    reader_v1_0 = _command_envelope(
        "1.0",
        {
            "access_mode": "read_write",
            "idempotency_key": "fixture-command-0001",
            "kind": "workspace.open",
            "repository_fingerprint_sha256": "0" * 64,
        },
    )
    reader_v1_0["expected_sequence"] = 0
    reader_v1_1 = _command_envelope(
        "1.1",
        {
            "kind": "task.inspect",
            "task_id": "tsk_0123456789abcdef0123456789abcdef",
        },
    )
    reader_v1_2 = _command_envelope(
        CURRENT_SCHEMA_VERSION,
        {
            "after_sequence": 42,
            "event_types": ["Artifact.Durable", "Turn.Completed"],
            "kind": "event.subscribe",
        },
    )
    rollback_v1_0 = {
        "event_id": "evt_0123456789abcdef0123456789abcdef",
        "event_type": "Future.Checkpointed",
        "future_evidence": {
            "hash": "1" * 64,
            "unknown_fields": ["retained", "without", "projection"],
        },
        "schema_version": CURRENT_SCHEMA_VERSION,
    }
    rollback_v1_1 = _command_envelope(
        CURRENT_SCHEMA_VERSION,
        {
            "destination": "future-overlay",
            "kind": "turn.teleport",
            "unknown_policy": {"mode": "preserve-only"},
        },
    )
    return {
        READER_V1_0_PATH: reader_v1_0,
        READER_V1_1_PATH: reader_v1_1,
        READER_V1_2_PATH: reader_v1_2,
        ROLLBACK_V1_0_PATH: rollback_v1_0,
        ROLLBACK_V1_1_PATH: rollback_v1_1,
    }
