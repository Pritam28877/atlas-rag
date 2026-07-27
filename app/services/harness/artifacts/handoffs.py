"""Typed producer handoffs and dependency-only artifact access."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol.base import (
    ArtifactId,
    BoundedLabel,
    BoundedReason,
    InlineText,
    MediaType,
    Sha256,
    StrictProtocolModel,
    TaskId,
    UtcTimestamp,
    WorkspaceId,
)

MAXIMUM_HANDOFFS = 1_024
MAXIMUM_HANDOFF_EVIDENCE = 256
MAXIMUM_HANDOFF_CLAIMS = 64
MAXIMUM_HANDOFF_CHANGES = 256
MAXIMUM_HANDOFF_VERIFICATIONS = 64
MAXIMUM_DEPENDENCY_ARTIFACTS = 1_024

type HandoffPath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=512, pattern=r"^[^\x00]+$"),
]


class HandoffOutcome(StrEnum):
    DRAFT = "draft"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class HandoffChangeKind(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"


class HandoffEvidence(StrictProtocolModel):
    artifact_id: ArtifactId
    producer_task_id: TaskId
    workspace_id: WorkspaceId
    media_type: MediaType
    size_bytes: int = Field(ge=1, le=16 * 1024 * 1024)
    content_sha256: Sha256


class HandoffClaim(StrictProtocolModel):
    claim_id: BoundedLabel
    statement: InlineText
    evidence_artifact_ids: tuple[ArtifactId, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_HANDOFF_EVIDENCE,
    )
    claim_sha256: Sha256

    @model_validator(mode="after")
    def validate_claim(self) -> Self:
        _require_sorted_unique(self.evidence_artifact_ids, "claim evidence")
        expected = _claim_sha256(self.statement, self.evidence_artifact_ids)
        if self.claim_sha256 != expected:
            raise ValueError("handoff claim hash is invalid")
        return self


class HandoffChange(StrictProtocolModel):
    path: HandoffPath
    kind: HandoffChangeKind
    before_sha256: Sha256 | None = None
    after_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_change(self) -> Self:
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != self.path:
            raise ValueError("handoff change path is not canonical")
        if self.kind is HandoffChangeKind.ADDED and self.before_sha256 is not None:
            raise ValueError("added change cannot contain a before hash")
        if self.kind is HandoffChangeKind.DELETED and self.after_sha256 is not None:
            raise ValueError("deleted change cannot contain an after hash")
        if self.kind is HandoffChangeKind.MODIFIED and (
            self.before_sha256 is None or self.after_sha256 is None
        ):
            raise ValueError("modified change requires before and after hashes")
        if self.kind is not HandoffChangeKind.DELETED and self.after_sha256 is None:
            raise ValueError("non-deleted change requires an after hash")
        return self


class HandoffVerification(StrictProtocolModel):
    verifier_task_id: TaskId
    gate: BoundedLabel
    status: VerificationStatus
    evidence_artifact_ids: tuple[ArtifactId, ...] = Field(
        max_length=MAXIMUM_HANDOFF_EVIDENCE,
    )
    reason: BoundedReason | None = None

    @model_validator(mode="after")
    def validate_verification(self) -> Self:
        _require_sorted_unique(self.evidence_artifact_ids, "verification evidence")
        if self.status is VerificationStatus.PASSED:
            if not self.evidence_artifact_ids or self.reason is not None:
                raise ValueError("passed verification requires evidence only")
        elif self.reason is None:
            raise ValueError("failed or skipped verification requires a reason")
        return self


class HandoffRecord(StrictProtocolModel):
    handoff_id: ArtifactId
    producer_task_id: TaskId
    consumer_task_id: TaskId
    workspace_id: WorkspaceId
    dependency_task_ids: tuple[TaskId, ...] = Field(max_length=64)
    evidence: tuple[HandoffEvidence, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_HANDOFF_EVIDENCE,
    )
    claims: tuple[HandoffClaim, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_HANDOFF_CLAIMS,
    )
    changes: tuple[HandoffChange, ...] = Field(max_length=MAXIMUM_HANDOFF_CHANGES)
    verifications: tuple[HandoffVerification, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_HANDOFF_VERIFICATIONS,
    )
    risks: tuple[BoundedReason, ...] = Field(max_length=32)
    not_checked: tuple[BoundedReason, ...] = Field(min_length=1, max_length=32)
    outcome: HandoffOutcome
    created_at: UtcTimestamp
    handoff_sha256: Sha256

    @model_validator(mode="after")
    def validate_handoff(self) -> Self:
        _require_sorted_unique(self.dependency_task_ids, "handoff dependencies")
        _require_sorted_unique(
            tuple(item.artifact_id for item in self.evidence),
            "handoff evidence",
        )
        _require_sorted_unique(
            tuple(item.claim_id for item in self.claims),
            "handoff claims",
        )
        _require_sorted_unique(
            tuple(item.path for item in self.changes),
            "handoff changes",
        )
        evidence_ids = {item.artifact_id for item in self.evidence}
        for claim in self.claims:
            if not set(claim.evidence_artifact_ids).issubset(evidence_ids):
                raise ValueError("claim references unavailable evidence")
        for verification in self.verifications:
            if verification.verifier_task_id == self.producer_task_id:
                raise ValueError("producer cannot independently verify its handoff")
            if not set(verification.evidence_artifact_ids).issubset(evidence_ids):
                raise ValueError("verification references unavailable evidence")
        evidence_tasks = {item.producer_task_id for item in self.evidence}
        if not evidence_tasks.issubset(set(self.dependency_task_ids)):
            raise ValueError("handoff evidence must come from dependencies")
        if self.outcome is HandoffOutcome.ACCEPTED and any(
            verification.status is not VerificationStatus.PASSED
            for verification in self.verifications
        ):
            raise ValueError("accepted handoff requires passing verification")
        expected = handoff_sha256(self.model_dump(mode="json"))
        if self.handoff_sha256 != expected:
            raise ValueError("handoff hash is invalid")
        return self


class DependencyArtifact(StrictProtocolModel):
    artifact_id: ArtifactId
    producer_task_id: TaskId
    workspace_id: WorkspaceId
    media_type: MediaType
    size_bytes: int = Field(ge=1, le=16 * 1024 * 1024)
    content_sha256: Sha256


class DependencyArtifactCatalog:
    """Authorizes artifact reads only through declared task dependencies."""

    def __init__(self) -> None:
        self._artifacts: dict[str, DependencyArtifact] = {}
        self._dependencies: dict[str, frozenset[str]] = {}

    def register_dependencies(
        self,
        consumer_task_id: TaskId,
        dependency_task_ids: tuple[TaskId, ...],
    ) -> None:
        _require_sorted_unique(dependency_task_ids, "dependency tasks")
        self._dependencies[consumer_task_id] = frozenset(dependency_task_ids)

    def register(self, artifact: DependencyArtifact) -> None:
        if (
            artifact.artifact_id not in self._artifacts
            and len(self._artifacts) >= MAXIMUM_DEPENDENCY_ARTIFACTS
        ):
            raise ValueError("dependency artifact capacity is exhausted")
        existing = self._artifacts.get(artifact.artifact_id)
        if existing is not None and existing != artifact:
            raise ValueError("artifact identity collision")
        self._artifacts[artifact.artifact_id] = artifact

    def authorize(
        self,
        consumer_task_id: TaskId,
        artifact_id: ArtifactId,
        workspace_id: WorkspaceId,
    ) -> DependencyArtifact:
        artifact = self._artifacts.get(artifact_id)
        dependencies = self._dependencies.get(consumer_task_id, frozenset())
        if artifact is None or artifact.producer_task_id not in dependencies:
            raise PermissionError("artifact is outside consumer dependencies")
        if artifact.workspace_id != workspace_id:
            raise PermissionError("artifact workspace scope does not match")
        return artifact


class InMemoryHandoffStore:
    """Bounded handoff repository with dependency and acceptance checks."""

    def __init__(self, catalog: DependencyArtifactCatalog) -> None:
        self._catalog = catalog
        self._records: dict[str, HandoffRecord] = {}

    def save(self, record: HandoffRecord) -> HandoffRecord:
        if (
            record.handoff_id not in self._records
            and len(self._records) >= MAXIMUM_HANDOFFS
        ):
            raise ValueError("handoff capacity is exhausted")
        for evidence in record.evidence:
            self._catalog.authorize(
                record.consumer_task_id,
                evidence.artifact_id,
                record.workspace_id,
            )
        existing = self._records.get(record.handoff_id)
        if existing is not None and existing != record:
            raise ValueError("handoff identity collision")
        self._records[record.handoff_id] = record
        return record

    def get(self, handoff_id: ArtifactId) -> HandoffRecord:
        record = self._records.get(handoff_id)
        if record is None:
            raise KeyError("handoff does not exist")
        return record


def build_handoff_record(
    *,
    handoff_id: ArtifactId,
    producer_task_id: TaskId,
    consumer_task_id: TaskId,
    workspace_id: WorkspaceId,
    dependency_task_ids: tuple[TaskId, ...],
    evidence: tuple[HandoffEvidence, ...],
    claims: tuple[HandoffClaim, ...],
    changes: tuple[HandoffChange, ...],
    verifications: tuple[HandoffVerification, ...],
    risks: tuple[BoundedReason, ...],
    not_checked: tuple[BoundedReason, ...],
    outcome: HandoffOutcome,
    created_at: UtcTimestamp,
) -> HandoffRecord:
    provisional = HandoffRecord.model_construct(
        handoff_id=handoff_id,
        producer_task_id=producer_task_id,
        consumer_task_id=consumer_task_id,
        workspace_id=workspace_id,
        dependency_task_ids=dependency_task_ids,
        evidence=evidence,
        claims=claims,
        changes=changes,
        verifications=verifications,
        risks=risks,
        not_checked=not_checked,
        outcome=outcome,
        created_at=created_at,
    )
    return HandoffRecord(
        handoff_id=handoff_id,
        producer_task_id=producer_task_id,
        consumer_task_id=consumer_task_id,
        workspace_id=workspace_id,
        dependency_task_ids=dependency_task_ids,
        evidence=evidence,
        claims=claims,
        changes=changes,
        verifications=verifications,
        risks=risks,
        not_checked=not_checked,
        outcome=outcome,
        created_at=created_at,
        handoff_sha256=handoff_sha256(
            provisional.model_dump(mode="json", warnings=False)
        ),
    )


def handoff_sha256(values: object) -> str:
    if isinstance(values, dict):
        values = {
            key: value
            for key, value in values.items()
            if key != "handoff_sha256"
        }
    return hashlib.sha256(
        json.dumps(
            values,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _claim_sha256(statement: str, evidence_ids: Iterable[str]) -> str:
    return hashlib.sha256(
        json.dumps(
            {"evidence_artifact_ids": list(evidence_ids), "statement": statement},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _require_sorted_unique(values: Iterable[str], field_name: str) -> None:
    values_tuple = tuple(values)
    if tuple(sorted(set(values_tuple))) != values_tuple:
        raise ValueError(f"{field_name} must be unique and sorted")
