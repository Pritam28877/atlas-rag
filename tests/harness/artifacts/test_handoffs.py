"""Typed handoff and dependency-artifact access tests."""

import hashlib
from datetime import UTC, datetime

import pytest

from app.services.harness.artifacts import (
    DependencyArtifact,
    DependencyArtifactCatalog,
    HandoffChange,
    HandoffChangeKind,
    HandoffClaim,
    HandoffEvidence,
    HandoffOutcome,
    HandoffRecord,
    HandoffVerification,
    InMemoryHandoffStore,
    VerificationStatus,
    build_handoff_record,
)

WORKSPACE = "wsp_" + "2" * 32
PRODUCER = "tsk_" + "3" * 32
CONSUMER = "tsk_" + "4" * 32
VERIFIER = "tsk_" + "5" * 32
UNRELATED = "tsk_" + "6" * 32
ARTIFACT = "art_" + "7" * 32
NOW = datetime(2026, 1, 1, tzinfo=UTC)
CONTENT = hashlib.sha256(b"evidence").hexdigest()


def _evidence() -> HandoffEvidence:
    return HandoffEvidence(
        artifact_id=ARTIFACT,
        producer_task_id=PRODUCER,
        workspace_id=WORKSPACE,
        media_type="text/plain",
        size_bytes=8,
        content_sha256=CONTENT,
    )


def _claim() -> HandoffClaim:
    statement = "The producer completed the bounded change."
    return HandoffClaim(
        claim_id="claim-complete",
        statement=statement,
        evidence_artifact_ids=(ARTIFACT,),
        claim_sha256=hashlib.sha256(
            ('{"evidence_artifact_ids":["'
             + ARTIFACT
             + '"],"statement":"'
             + statement
             + '"}').encode()
        ).hexdigest(),
    )


def _record(outcome: HandoffOutcome = HandoffOutcome.ACCEPTED) -> HandoffRecord:
    return build_handoff_record(
        handoff_id="art_" + "8" * 32,
        producer_task_id=PRODUCER,
        consumer_task_id=CONSUMER,
        workspace_id=WORKSPACE,
        dependency_task_ids=(PRODUCER, VERIFIER),
        evidence=(_evidence(),),
        claims=(_claim(),),
        changes=(
            HandoffChange(
                path="src/app.py",
                kind=HandoffChangeKind.MODIFIED,
                before_sha256="a" * 64,
                after_sha256="b" * 64,
            ),
        ),
        verifications=(
            HandoffVerification(
                verifier_task_id=VERIFIER,
                gate="tests",
                status=VerificationStatus.PASSED,
                evidence_artifact_ids=(ARTIFACT,),
            ),
        ),
        risks=(),
        not_checked=("manual review",),
        outcome=outcome,
        created_at=NOW,
    )


def test_accepted_handoff_requires_explicit_evidence_and_verification() -> None:
    catalog = DependencyArtifactCatalog()
    catalog.register_dependencies(CONSUMER, (PRODUCER, VERIFIER))
    catalog.register(
        DependencyArtifact(
            artifact_id=ARTIFACT,
            producer_task_id=PRODUCER,
            workspace_id=WORKSPACE,
            media_type="text/plain",
            size_bytes=8,
            content_sha256=CONTENT,
        )
    )
    record = _record()
    assert InMemoryHandoffStore(catalog).save(record) == record

    invalid_values = record.model_dump(mode="python")
    invalid_values["not_checked"] = ()
    invalid_values.pop("handoff_sha256")
    with pytest.raises(ValueError, match="at least 1"):
        build_handoff_record(**invalid_values)


def test_unrelated_dependency_artifact_is_denied() -> None:
    catalog = DependencyArtifactCatalog()
    catalog.register_dependencies(CONSUMER, (UNRELATED,))
    catalog.register(
        DependencyArtifact(
            artifact_id=ARTIFACT,
            producer_task_id=PRODUCER,
            workspace_id=WORKSPACE,
            media_type="text/plain",
            size_bytes=8,
            content_sha256=CONTENT,
        )
    )
    with pytest.raises(PermissionError, match="outside consumer dependencies"):
        catalog.authorize(CONSUMER, ARTIFACT, WORKSPACE)


def test_producer_cannot_self_verify_and_hashes_are_bound() -> None:
    values = _record().model_dump(mode="python")
    values["verifications"] = (
        HandoffVerification(
            verifier_task_id=PRODUCER,
            gate="tests",
            status=VerificationStatus.PASSED,
            evidence_artifact_ids=(ARTIFACT,),
        ),
    )
    values.pop("handoff_sha256")
    with pytest.raises(ValueError, match="independently verify"):
        build_handoff_record(**values)
