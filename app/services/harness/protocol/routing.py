"""Provider routing requirements, candidates, and decision evidence."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol.base import (
    BoundedReason,
    Capability,
    ProviderDecisionId,
    Sha256,
    StrictProtocolModel,
    TurnId,
    UtcTimestamp,
)
from app.services.harness.protocol.conversation import DataClassification

MAXIMUM_PROVIDER_ROUTE_COST_MICROUSD = 25_120_000_000

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
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


class ProviderRouteRejectionCode(StrEnum):
    CAPABILITY = "capability"
    CONTEXT_FEATURE = "context_feature"
    CONTEXT_WINDOW = "context_window"
    COST = "cost"
    DATA_CLASSIFICATION = "data_classification"
    HEALTH = "health"
    INPUT_MODALITY = "input_modality"
    OUTPUT_LIMIT = "output_limit"
    OUTPUT_MODALITY = "output_modality"
    PRICE = "price"
    REGION = "region"
    RETENTION = "retention"
    TRAINING = "training"


class ProviderRequirements(StrictProtocolModel):
    input_tokens: int = Field(ge=1, le=2_000_000)
    reserved_output_tokens: int = Field(ge=1, le=512_000)
    required_capabilities: tuple[Capability, ...] = Field(max_length=64)
    required_input_modalities: tuple[Capability, ...] = Field(
        default=(),
        max_length=16,
    )
    required_output_modalities: tuple[Capability, ...] = Field(
        default=(),
        max_length=16,
    )
    required_context_features: tuple[Capability, ...] = Field(
        default=(),
        max_length=16,
    )
    data_classification: DataClassification
    allowed_regions: tuple[Region, ...] = Field(min_length=1, max_length=32)
    max_retention_days: int = Field(ge=0, le=3650)
    allow_training: bool
    max_cost_microusd: int = Field(ge=0, le=10_000_000_000)

    @model_validator(mode="after")
    def validate_canonical_values(self) -> Self:
        for values, name in (
            (self.required_capabilities, "required capabilities"),
            (self.required_input_modalities, "required input modalities"),
            (self.required_output_modalities, "required output modalities"),
            (self.required_context_features, "required context features"),
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
    priority: int = Field(default=0, ge=0, le=1_000_000)
    capabilities: tuple[Capability, ...] = Field(max_length=64)
    input_modalities: tuple[Capability, ...] = Field(
        default=(),
        max_length=16,
    )
    output_modalities: tuple[Capability, ...] = Field(
        default=(),
        max_length=16,
    )
    context_features: tuple[Capability, ...] = Field(
        default=(),
        max_length=16,
    )
    accepted_data_classifications: tuple[DataClassification, ...] = Field(
        min_length=1,
        max_length=4,
    )
    retention_days: int = Field(ge=0, le=3650)
    training_enabled: bool
    destination_sha256: Sha256
    context_window_tokens: int = Field(ge=1, le=2_000_000)
    max_output_tokens: int = Field(ge=1, le=512_000)
    estimated_cost_microusd: int = Field(
        ge=0,
        le=MAXIMUM_PROVIDER_ROUTE_COST_MICROUSD,
    )
    health_snapshot_sha256: Sha256
    price_version_sha256: Sha256
    price_active: bool = True

    @model_validator(mode="after")
    def validate_capabilities(self) -> Self:
        for values, label in (
            (self.capabilities, "route capabilities"),
            (self.input_modalities, "route input modalities"),
            (self.output_modalities, "route output modalities"),
            (self.context_features, "route context features"),
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{label} must be unique and sorted")
        classifications = self.accepted_data_classifications
        if tuple(sorted(set(classifications))) != classifications:
            raise ValueError("route data classifications must be unique and sorted")
        return self


class RejectedProviderRoute(StrictProtocolModel):
    route_id: RouteId
    reason: BoundedReason
    rejection_codes: tuple[ProviderRouteRejectionCode, ...] = Field(
        default=(),
        max_length=16,
    )
    provider: ProviderName | None = None
    model: ModelName | None = None
    model_revision_sha256: Sha256 | None = None
    health: RouteHealth | None = None
    health_snapshot_sha256: Sha256 | None = None
    price_version_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_rejection_evidence(self) -> Self:
        if (
            tuple(sorted(set(self.rejection_codes)))
            != self.rejection_codes
        ):
            raise ValueError("route rejection codes must be unique and sorted")
        evidence = (
            self.provider,
            self.model,
            self.model_revision_sha256,
            self.health,
            self.health_snapshot_sha256,
            self.price_version_sha256,
        )
        if any(value is not None for value in evidence) and not all(
            value is not None for value in evidence
        ):
            raise ValueError("rejected route revision evidence must be complete")
        return self


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
        missing_input_modalities = set(
            self.requirements.required_input_modalities
        ).difference(route.input_modalities)
        missing_output_modalities = set(
            self.requirements.required_output_modalities
        ).difference(route.output_modalities)
        missing_context_features = set(
            self.requirements.required_context_features
        ).difference(route.context_features)
        invalid_route = (
            route.health not in {RouteHealth.HEALTHY, RouteHealth.DEGRADED}
            or route.region not in self.requirements.allowed_regions
            or route.context_window_tokens < required_context
            or route.max_output_tokens
            < self.requirements.reserved_output_tokens
            or route.estimated_cost_microusd
            > self.requirements.max_cost_microusd
            or not route.price_active
            or bool(missing_capabilities)
            or bool(missing_input_modalities)
            or bool(missing_output_modalities)
            or bool(missing_context_features)
            or self.requirements.data_classification
            not in route.accepted_data_classifications
            or route.retention_days > self.requirements.max_retention_days
            or (route.training_enabled and not self.requirements.allow_training)
        )
        if invalid_route:
            raise ValueError(f"eligible route {route.route_id} violates requirements")
