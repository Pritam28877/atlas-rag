"""Deterministic provider evaluation records and bounded evidence."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedLabel,
    BoundedReason,
    EvaluationId,
    EventId,
    ResourceUsage,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.states import EvaluationState


class EvaluationMetric(StrictProtocolModel):
    name: BoundedLabel
    score_ppm: int = Field(ge=0, le=1_000_000)


class EvaluationFailure(StrictProtocolModel):
    fixture_sha256: Sha256
    reason: BoundedReason


class EvaluationRunRecord(StrictProtocolModel):
    """Pinned evaluation evidence without floating-point scores."""

    evaluation_id: EvaluationId
    state: EvaluationState
    harness_revision_sha256: Sha256
    model_revision_sha256: Sha256
    configuration_sha256: Sha256
    fixture_sha256s: tuple[Sha256, ...] = Field(
        min_length=1,
        max_length=10_000,
    )
    metrics: tuple[EvaluationMetric, ...] = Field(max_length=256)
    failures: tuple[EvaluationFailure, ...] = Field(max_length=10_000)
    usage: ResourceUsage
    trace_event_ids: tuple[EventId, ...] = Field(max_length=10_000)
    created_at: UtcTimestamp
    started_at: UtcTimestamp | None = None
    completed_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def validate_run(self) -> Self:
        self._require_unique_sorted(self.fixture_sha256s, "fixture hashes")
        self._require_unique_sorted(self.trace_event_ids, "trace event IDs")
        metric_names = tuple(metric.name for metric in self.metrics)
        self._require_unique_sorted(metric_names, "metric names")
        requires_start = self.state in {
            EvaluationState.RUNNING,
            EvaluationState.COMPLETED,
            EvaluationState.FAILED,
        }
        if requires_start and self.started_at is None:
            raise ValueError("started evaluation state requires started_at")
        if self.state is EvaluationState.PENDING and self.started_at is not None:
            raise ValueError("pending evaluation cannot contain started_at")
        terminal = self.state in {
            EvaluationState.COMPLETED,
            EvaluationState.FAILED,
            EvaluationState.CANCELLED,
        }
        if terminal != (self.completed_at is not None):
            raise ValueError("terminal evaluation state requires completed_at")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("evaluation start cannot precede creation")
        if self.completed_at is not None and self.started_at is not None:
            if self.completed_at < self.started_at:
                raise ValueError("evaluation completion cannot precede start")
        if self.completed_at is not None and self.started_at is None:
            if self.completed_at < self.created_at:
                raise ValueError("evaluation completion cannot precede creation")
        if self.state is EvaluationState.COMPLETED and not self.metrics:
            raise ValueError("completed evaluation requires at least one metric")
        if self.state is EvaluationState.FAILED and not self.failures:
            raise ValueError("failed evaluation requires failure evidence")
        fixture_hashes = set(self.fixture_sha256s)
        if any(
            failure.fixture_sha256 not in fixture_hashes
            for failure in self.failures
        ):
            raise ValueError("evaluation failure must reference a run fixture")
        return self

    @staticmethod
    def _require_unique_sorted(values: tuple[str, ...], name: str) -> None:
        if tuple(sorted(set(values))) != values:
            raise ValueError(f"{name} must be unique and sorted")
