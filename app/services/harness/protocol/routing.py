"""Provider routing evidence and deterministic evaluation records."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol.base import (
    BoundedLabel,
    BoundedReason,
    Capability,
    EvaluationId,
    EventId,
    ProviderDecisionId,
    ResourceUsage,
    Sha256,
    StrictProtocolModel,
    TurnId,
    UtcTimestamp,
)
from app.services.harness.protocol.conversation import DataClassification
from app.services.harness.protocol.states import EvaluationState

type RouteId = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$",
    ),
]
type ProviderName = Annotated[
    str,
    StringConstraints(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
    ),
]
type ModelName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^[^\x00-\x20\x7f]+$"),
]
type Region = Annotated[
    str,
    StringConstraints(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    ),
]


class RouteHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"


class ProviderRequirements(StrictProtocolModel):
    input_tokens: int = Field(ge=1, le=2_000_000)
    reserved_output_tokens: int = Field(ge=1, le=512_000)
    required_capabilities: tuple[Capability, ...] = Field(max_length=64)
    data_classification: DataClassification
    allowed_regions: tuple[Region, ...] = Field(min_length=1, max_length=32)
    max_retention_days: int = Field(ge=0, le=3650)
    allow_training: bool
    max_cost_microusd: int = Field(ge=0, le=10_000_000_000)

    @model_validator(mode="after")
    def validate_canonical_values(self) -> Self:
        for values, name in (
            (self.required_capabilities, "required capabilities"),
            (self.allowed_regions, "allowed regions"),
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{name} must be unique and sorted")
        return self


class ProviderRoute(StrictProtocolModel):
    route_id: RouteId
    provider: ProviderName
    model: ModelName
    model_revision_sha256: Sha256
    region: Region
    health: RouteHealth
    capabilities: tuple[Capability, ...] = Field(max_length=64)
    accepted_data_classifications: tuple[DataClassification, ...] = Field(
        min_length=1,
        max_length=4,
    )
    retention_days: int = Field(ge=0, le=3650)
    training_enabled: bool
    destination_sha256: Sha256
    context_window_tokens: int = Field(ge=1, le=2_000_000)
    max_output_tokens: int = Field(ge=1, le=512_000)
    estimated_cost_microusd: int = Field(ge=0, le=10_000_000_000)
    health_snapshot_sha256: Sha256
    price_version_sha256: Sha256

    @model_validator(mode="after")
    def validate_capabilities(self) -> Self:
        if tuple(sorted(set(self.capabilities))) != self.capabilities:
            raise ValueError("route capabilities must be unique and sorted")
        classifications = self.accepted_data_classifications
        if tuple(sorted(set(classifications))) != classifications:
            raise ValueError("route data classifications must be unique and sorted")
        return self


class RejectedProviderRoute(StrictProtocolModel):
    route_id: RouteId
    reason: BoundedReason


class ProviderRouteDecisionRecord(StrictProtocolModel):
    """Complete eligible/eliminated route evidence for one turn decision."""

    provider_decision_id: ProviderDecisionId
    turn_id: TurnId
    requirements: ProviderRequirements
    eligible_routes: tuple[ProviderRoute, ...] = Field(max_length=64)
    rejected_routes: tuple[RejectedProviderRoute, ...] = Field(max_length=256)
    selected_route_id: RouteId | None = None
    selection_reason: BoundedReason
    decided_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_route_partition(self) -> Self:
        eligible_ids = tuple(route.route_id for route in self.eligible_routes)
        rejected_ids = tuple(route.route_id for route in self.rejected_routes)
        if tuple(sorted(set(eligible_ids))) != eligible_ids:
            raise ValueError("eligible route IDs must be unique and sorted")
        if tuple(sorted(set(rejected_ids))) != rejected_ids:
            raise ValueError("rejected route IDs must be unique and sorted")
        if set(eligible_ids).intersection(rejected_ids):
            raise ValueError("a provider route cannot be eligible and rejected")
        if self.selected_route_id is not None:
            if self.selected_route_id not in eligible_ids:
                raise ValueError("selected provider route must be eligible")
        elif eligible_ids:
            raise ValueError("an eligible route set requires a selected route")
        for route in self.eligible_routes:
            self._validate_eligible_route(route)
        return self

    def _validate_eligible_route(self, route: ProviderRoute) -> None:
        required_context = (
            self.requirements.input_tokens
            + self.requirements.reserved_output_tokens
        )
        missing_capabilities = set(
            self.requirements.required_capabilities
        ).difference(route.capabilities)
        invalid_route = (
            route.region not in self.requirements.allowed_regions
            or route.context_window_tokens < required_context
            or route.max_output_tokens
            < self.requirements.reserved_output_tokens
            or route.estimated_cost_microusd
            > self.requirements.max_cost_microusd
            or bool(missing_capabilities)
            or self.requirements.data_classification
            not in route.accepted_data_classifications
            or route.retention_days > self.requirements.max_retention_days
            or (route.training_enabled and not self.requirements.allow_training)
        )
        if invalid_route:
            raise ValueError(f"eligible route {route.route_id} violates requirements")


class EvaluationMetric(StrictProtocolModel):
    name: BoundedLabel
    score_ppm: int = Field(ge=0, le=1_000_000)


class EvaluationFailure(StrictProtocolModel):
    fixture_sha256: Sha256
    reason: BoundedReason


class EvaluationRunRecord(StrictProtocolModel):
    """Pinned evaluation evidence without floating-point scores or unbounded traces."""

    evaluation_id: EvaluationId
    state: EvaluationState
    harness_revision_sha256: Sha256
    model_revision_sha256: Sha256
    configuration_sha256: Sha256
    fixture_sha256s: tuple[Sha256, ...] = Field(min_length=1, max_length=10_000)
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
