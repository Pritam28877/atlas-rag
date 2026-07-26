import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import (
    CURRENT_SCHEMA_VERSION,
    READABLE_SCHEMA_VERSIONS,
    CommandEnvelope,
    CompatibilityAction,
    CompatibilityError,
    CompatibleRecordKind,
    EventRecord,
    PreservedUnknownRecord,
    compatibility_action,
    preserve_unknown_record,
    require_writer_rollback_compatibility,
)
from app.services.harness.protocol.compatibility import (
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_OPAQUE_RECORD_BYTES,
)
from scripts.harness_protocol_fixtures import (
    READER_V1_0_PATH,
    READER_V1_1_PATH,
    READER_V1_2_PATH,
    ROLLBACK_V1_0_PATH,
    ROLLBACK_V1_1_PATH,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "fixture_path",
    (READER_V1_0_PATH, READER_V1_1_PATH, READER_V1_2_PATH),
)
def test_current_reader_projects_current_and_previous_two_minors(
    fixture_path: Path,
) -> None:
    raw_json = (ROOT / fixture_path).read_bytes()
    envelope = CommandEnvelope.model_validate_json(raw_json)

    assert envelope.schema_version in READABLE_SCHEMA_VERSIONS
    assert (
        compatibility_action(
            envelope.schema_version,
            CURRENT_SCHEMA_VERSION,
        )
        is CompatibilityAction.PROJECT
    )


def test_rollback_reader_preserves_unknown_event_exactly() -> None:
    raw_json = (ROOT / ROLLBACK_V1_0_PATH).read_bytes()

    with pytest.raises(ValidationError):
        EventRecord.model_validate_json(raw_json)
    preserved = preserve_unknown_record(raw_json, "1.0")
    assert preserved.reader_action is CompatibilityAction.PRESERVE_OPAQUE
    assert preserved.record_kind is CompatibleRecordKind.EVENT
    assert preserved.record_type == "Future.Checkpointed"
    assert preserved.raw_json == raw_json
    assert preserved.content_sha256 == hashlib.sha256(raw_json).hexdigest()


def test_rollback_reader_preserves_unknown_command_and_fields_exactly() -> None:
    raw_json = (ROOT / ROLLBACK_V1_1_PATH).read_bytes()

    with pytest.raises(ValidationError, match="union_tag_invalid"):
        CommandEnvelope.model_validate_json(raw_json)
    preserved = preserve_unknown_record(raw_json, "1.1")
    assert preserved.reader_action is CompatibilityAction.PRESERVE_OPAQUE
    assert preserved.record_kind is CompatibleRecordKind.COMMAND
    assert preserved.record_type == "turn.teleport"
    assert preserved.raw_json == raw_json


def test_opaque_record_recomputes_hash_size_and_identity() -> None:
    raw_json = (ROOT / ROLLBACK_V1_0_PATH).read_bytes()
    preserved = preserve_unknown_record(raw_json, "1.0")
    values = preserved.model_dump()

    with pytest.raises(ValidationError, match="hash"):
        PreservedUnknownRecord.model_validate(
            {**values, "content_sha256": "0" * 64}
        )
    with pytest.raises(ValidationError, match="size"):
        PreservedUnknownRecord.model_validate(
            {**values, "size_bytes": preserved.size_bytes - 1}
        )
    with pytest.raises(ValidationError, match="identity"):
        PreservedUnknownRecord.model_validate(
            {**values, "record_type": "Future.Different"}
        )


def test_opaque_parser_rejects_duplicate_keys_and_non_json_numbers() -> None:
    duplicate = (
        b'{"schema_version":"1.2","event_type":"Future.One",'
        b'"event_type":"Future.Two"}'
    )
    non_json_number = (
        b'{"schema_version":"1.2","event_type":"Future.One","value":NaN}'
    )

    with pytest.raises(CompatibilityError, match="duplicate JSON key"):
        preserve_unknown_record(duplicate, "1.1")
    with pytest.raises(CompatibilityError, match="non-JSON numeric"):
        preserve_unknown_record(non_json_number, "1.1")


def test_opaque_parser_enforces_byte_depth_and_node_bounds() -> None:
    oversized = b"{" + b"x" * MAX_OPAQUE_RECORD_BYTES + b"}"
    nested: object = "leaf"
    for _ in range(MAX_JSON_DEPTH + 1):
        nested = {"child": nested}
    deep_record = {
        "event_type": "Future.Deep",
        "payload": nested,
        "schema_version": "1.2",
    }
    node_record = {
        "event_type": "Future.Wide",
        "payload": [0] * MAX_JSON_NODES,
        "schema_version": "1.2",
    }

    with pytest.raises(CompatibilityError, match="size"):
        preserve_unknown_record(oversized, "1.1")
    with pytest.raises(CompatibilityError, match="depth"):
        preserve_unknown_record(
            json.dumps(deep_record).encode(),
            "1.1",
        )
    with pytest.raises(CompatibilityError, match="node"):
        preserve_unknown_record(
            json.dumps(node_record).encode(),
            "1.1",
        )


def test_compatibility_matrix_and_writer_gate_fail_closed() -> None:
    assert compatibility_action("1.0", "1.2") is CompatibilityAction.PROJECT
    assert (
        compatibility_action("1.2", "1.0")
        is CompatibilityAction.PRESERVE_OPAQUE
    )
    assert compatibility_action("1.3", "1.2") is CompatibilityAction.REJECT
    assert require_writer_rollback_compatibility(
        "1.2",
        ("1.0", "1.1"),
    ) == (
        CompatibilityAction.PRESERVE_OPAQUE,
        CompatibilityAction.PRESERVE_OPAQUE,
    )

    with pytest.raises(CompatibilityError, match="unsafe"):
        require_writer_rollback_compatibility("1.3", ("1.0", "1.1"))
    with pytest.raises(CompatibilityError, match="unique and sorted"):
        require_writer_rollback_compatibility("1.2", ("1.1", "1.0"))
