"""Model, policy, failure, and price evidence shared by provider adapters."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol.base import (
    BoundedReason,
    Capability,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.conversation import DataClassification
from app.services.harness.protocol.provider_request import ProviderModality
from app.services.harness.protocol.provider_stream import (
    ProviderFailureClass,
    ProviderTokenUsage,
)
from app.services.harness.protocol.routing import (
    ModelName,
    ProviderName,
    Region,
    RouteId,
)

type ProviderCredentialHandle = Annotated[
    str,
    StringConstraints(pattern=r"^pcr_[0-9a-f]{32}$"),
]
type ProviderErrorCode = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class ProviderContextFeature(StrEnum):
    NATIVE_COMPACTION = "native_compaction"
    PROMPT_CACHE = "prompt_cache"
    STATE_REFERENCE = "state_reference"


class ProviderRetryDisposition(StrEnum):
    ELIGIBLE = "eligible"
    PROHIBITED = "prohibited"


class ProviderModelCapabilities(StrictProtocolModel):
    provider: ProviderName
    model: ModelName
    model_revision_sha256: Sha256
    catalog_snapshot_sha256: Sha256
    capabilities: tuple[Capability, ...] = Field(max_length=64)
    input_modalities: tuple[ProviderModality, ...] = Field(
        min_length=1,
        max_length=5,
    )
    output_modalities: tuple[ProviderModality, ...] = Field(
        min_length=1,
        max_length=5,
    )
    context_features: tuple[ProviderContextFeature, ...] = Field(max_length=3)
    available_regions: tuple[Region, ...] = Field(
        min_length=1,
        max_length=32,
    )
    context_window_tokens: int = Field(ge=1, le=2_000_000)
    max_output_tokens: int = Field(ge=1, le=512_000)
    observed_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_canonical_capabilities(self) -> Self:
        for values, label in (
            (self.capabilities, "model capabilities"),
            (self.input_modalities, "input modalities"),
            (self.output_modalities, "output modalities"),
            (self.context_features, "context features"),
            (self.available_regions, "available regions"),
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{label} must be unique and sorted")
        if self.max_output_tokens > self.context_window_tokens:
            raise ValueError("model output limit exceeds its context window")
        return self


class ProviderDataPolicy(StrictProtocolModel):
    provider: ProviderName
    policy_revision_sha256: Sha256
    destination_sha256: Sha256
    accepted_classifications: tuple[DataClassification, ...] = Field(
        min_length=1,
        max_length=4,
    )
    allowed_regions: tuple[Region, ...] = Field(min_length=1, max_length=32)
    retention_days: int = Field(ge=0, le=3650)
    training_enabled: bool
    observed_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_canonical_policy(self) -> Self:
        for values, label in (
            (self.accepted_classifications, "accepted classifications"),
            (self.allowed_regions, "allowed regions"),
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{label} must be unique and sorted")
        return self


class ProviderPriceRecord(StrictProtocolModel):
    provider: ProviderName
    model: ModelName
    model_revision_sha256: Sha256
    region: Region
    price_version_sha256: Sha256
    input_microusd_per_million_tokens: int = Field(
        ge=0,
        le=10_000_000_000,
    )
    cached_input_microusd_per_million_tokens: int = Field(
        ge=0,
        le=10_000_000_000,
    )
    output_microusd_per_million_tokens: int = Field(
        ge=0,
        le=10_000_000_000,
    )
    reasoning_microusd_per_million_tokens: int = Field(
        ge=0,
        le=10_000_000_000,
    )
    effective_at: UtcTimestamp
    expires_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def validate_expiry(self) -> Self:
        if self.expires_at is not None and self.expires_at <= self.effective_at:
            raise ValueError("provider price expiry must follow effective time")
        return self


class ProviderUsageMetadata(StrictProtocolModel):
    route_id: RouteId
    provider_request_sha256: Sha256
    model_revision_sha256: Sha256
    price_version_sha256: Sha256
    usage: ProviderTokenUsage
    estimated_cost_microusd: int = Field(ge=0, le=10_000_000_000)
    recorded_at: UtcTimestamp


class ProviderTransportFailure(StrictProtocolModel):
    failure_class: ProviderFailureClass
    retry_disposition: ProviderRetryDisposition
    code: ProviderErrorCode
    reason: BoundedReason
    provider_request_sha256: Sha256
    http_status: int | None = Field(default=None, ge=100, le=599)
    retry_after_ms: int | None = Field(
        default=None,
        ge=0,
        le=3_600_000,
    )
    ambiguous: bool
    occurred_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_retry_evidence(self) -> Self:
        retryable_class = self.failure_class in {
            ProviderFailureClass.TRANSIENT,
            ProviderFailureClass.RATE_LIMIT,
        }
        eligible = (
            self.retry_disposition is ProviderRetryDisposition.ELIGIBLE
        )
        if eligible and not retryable_class:
            raise ValueError("failure class is not eligible for retry")
        if self.ambiguous and eligible:
            raise ValueError("ambiguous provider response cannot be retried")
        if self.retry_after_ms is not None and not eligible:
            raise ValueError("retry hint requires retry-eligible failure")
        return self


def price_is_active(
    price: ProviderPriceRecord,
    observed_at: datetime,
) -> bool:
    if (
        observed_at.tzinfo is None
        or observed_at.utcoffset() != timedelta(0)
    ):
        raise ValueError("price observation time must use UTC")
    if observed_at < price.effective_at:
        return False
    return price.expires_at is None or observed_at < price.expires_at
