from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import (
    ArtifactRecord,
    ArtifactState,
    DataClassification,
    InvalidTransitionError,
    require_artifact_transition,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def identifier(prefix: str) -> str:
    return f"{prefix}_0123456789abcdef0123456789abcdef"


def artifact(**overrides: object) -> ArtifactRecord:
    values: dict[str, object] = {
        "artifact_id": identifier("art"),
        "workspace_id": identifier("wsp"),
        "turn_id": identifier("trn"),
        "media_type": "application/json",
        "size_bytes": 1024,
        "content_sha256": "0" * 64,
        "storage_reference_sha256": "1" * 64,
        "classification": DataClassification.INTERNAL,
        "state": ArtifactState.STAGED,
        "created_at": NOW,
        "state_changed_at": NOW,
    }
    values.update(overrides)
    return ArtifactRecord.model_validate(values)


def test_artifact_retains_hashes_without_exposing_storage_location() -> None:
    record = artifact()

    assert record.content_sha256 == "0" * 64
    assert record.storage_reference_sha256 == "1" * 64
    assert "storage_uri" not in type(record).model_fields


def test_artifact_state_requires_matching_lifecycle_evidence() -> None:
    durable_at = NOW + timedelta(seconds=1)
    durable = artifact(
        state=ArtifactState.DURABLE,
        state_changed_at=durable_at,
        durable_at=durable_at,
    )
    assert durable.durable_at == durable_at

    with pytest.raises(ValidationError, match="requires durable_at"):
        artifact(
            state=ArtifactState.DURABLE,
            state_changed_at=durable_at,
        )
    with pytest.raises(ValidationError, match="requires deleted_at"):
        artifact(
            state=ArtifactState.DELETED,
            state_changed_at=durable_at,
            status_reason="Retention policy removed the content.",
        )
    with pytest.raises(ValidationError, match="match its state change"):
        artifact(
            state=ArtifactState.DELETED,
            state_changed_at=durable_at,
            deleted_at=NOW,
            status_reason="Retention policy removed the content.",
        )


def test_artifact_transitions_fail_closed_after_deletion() -> None:
    require_artifact_transition(ArtifactState.STAGED, ArtifactState.DURABLE)
    require_artifact_transition(ArtifactState.DURABLE, ArtifactState.DELETED)

    with pytest.raises(InvalidTransitionError):
        require_artifact_transition(
            ArtifactState.DELETED,
            ArtifactState.DURABLE,
        )
