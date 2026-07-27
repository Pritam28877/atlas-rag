"""Schema upgrade and rollback gate tests."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import CompatibilityAction
from app.services.harness.recovery import (
    plan_upgrade,
    plan_writer_gate,
    preserve_rollback_record,
)
from scripts.harness_protocol_fixtures import ROLLBACK_V1_0_PATH

ROOT = Path(__file__).resolve().parents[3]


def test_writer_gate_allows_declared_rollback_readers() -> None:
    decision = plan_writer_gate("1.2", ("1.0", "1.1", "1.2"))
    assert decision.allowed
    assert decision.actions == (
        CompatibilityAction.PRESERVE_OPAQUE,
        CompatibilityAction.PRESERVE_OPAQUE,
        CompatibilityAction.PROJECT,
    )


def test_writer_gate_blocks_unsupported_reader_without_fallback() -> None:
    decision = plan_writer_gate("1.2", ("1.0", "1.4"))
    assert not decision.allowed
    assert decision.reason is not None
    assert decision.actions == (
        CompatibilityAction.REJECT,
        CompatibilityAction.REJECT,
    )


def test_upgrade_plan_never_allows_journal_rewrite() -> None:
    plan = plan_upgrade(
        current_schema_version="1.1",
        target_schema_version="1.2",
        rollback_reader_versions=("1.0", "1.1"),
        preserved_unknown_records=2,
    )
    assert plan.writer_gate.allowed
    assert plan.journal_rewrite_allowed is False
    assert plan.preserved_unknown_records == 2


def test_rollback_preserves_unknown_record_bytes() -> None:
    raw_json = (ROOT / ROLLBACK_V1_0_PATH).read_bytes()
    preserved = preserve_rollback_record(raw_json, "1.0")
    assert preserved.raw_json == raw_json


def test_upgrade_plan_rejects_invalid_unknown_record_count() -> None:
    with pytest.raises(ValidationError, match="preserved_unknown_records"):
        plan_upgrade(
            current_schema_version="1.1",
            target_schema_version="1.2",
            rollback_reader_versions=("1.0",),
            preserved_unknown_records=-1,
        )
