"""Independently substitutable provider capability contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol, Self, TypeVar

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedReason,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.conversation import DataClassification
from app.services.harness.protocol.provider_capabilities import (
    ProviderContextFeature,
    ProviderCredentialHandle,
    ProviderDataPolicy,
    ProviderModelCapabilities,
    ProviderPriceRecord,
    ProviderTransportFailure,
    ProviderUsageMetadata,
)
from app.services.harness.protocol.provider_catalog import (
    ProviderCatalogPage,
    ProviderCatalogPageRequest,
    ProviderListPage,
    ProviderListPageRequest,
)
from app.services.harness.protocol.provider_request import (
    CanonicalProviderRequest,
)
from app.services.harness.protocol.provider_stream import (
    ProviderStreamBatch,
    ProviderTokenUsage,
)
from app.services.harness.protocol.routing import (
    ProviderName,
    Region,
    RouteId,
)

CredentialValueT = TypeVar("CredentialValueT")
CompiledRequestCovariantT = TypeVar(
    "CompiledRequestCovariantT",
    covariant=True,
)
CompiledRequestContravariantT = TypeVar(
    "CompiledRequestContravariantT",
    contravariant=True,
)
CredentialContravariantT = TypeVar(
    "CredentialContravariantT",
    contravariant=True,
)
WireEventCovariantT = TypeVar("WireEventCovariantT", covariant=True)
WireEventContravariantT = TypeVar(
    "WireEventContravariantT",
    contravariant=True,
)


class ProviderContextPlan(StrictProtocolModel):
    provider_request_sha256: Sha256
    model_revision_sha256: Sha256
    applied_features: tuple[ProviderContextFeature, ...] = Field(max_length=3)
    estimated_input_tokens: int = Field(ge=0, le=2_000_000)
    prompt_cache_key_sha256: Sha256 | None = None
    state_reference_sha256: Sha256 | None = None
    reason: BoundedReason

    @model_validator(mode="after")
    def validate_feature_evidence(self) -> Self:
        if tuple(sorted(set(self.applied_features))) != self.applied_features:
            raise ValueError("context features must be unique and sorted")
        has_prompt_cache = (
            ProviderContextFeature.PROMPT_CACHE in self.applied_features
        )
        if has_prompt_cache != (self.prompt_cache_key_sha256 is not None):
            raise ValueError("prompt cache feature requires cache key evidence")
        has_state_reference = (
            ProviderContextFeature.STATE_REFERENCE in self.applied_features
        )
        if has_state_reference != (self.state_reference_sha256 is not None):
            raise ValueError("state reference feature requires hash evidence")
        return self


class ProviderDataPolicyDecision(StrictProtocolModel):
    route_id: RouteId
    policy_revision_sha256: Sha256
    model_revision_sha256: Sha256
    classification: DataClassification
    region: Region
    allowed: bool
    reason: BoundedReason
    decided_at: UtcTimestamp


class ProviderTransportError(RuntimeError):
    def __init__(self, failure: ProviderTransportFailure) -> None:
        super().__init__("provider transport failed")
        self.failure = failure


class InferenceTransport(
    Protocol[
        CompiledRequestContravariantT,
        CredentialContravariantT,
        WireEventCovariantT,
    ]
):
    def stream(
        self,
        request: CompiledRequestContravariantT,
        credential: CredentialContravariantT,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> AsyncIterator[WireEventCovariantT]: ...


class ModelCatalog(Protocol):
    async def list_providers(
        self,
        request: ProviderListPageRequest,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> ProviderListPage: ...

    async def page(
        self,
        request: ProviderCatalogPageRequest,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> ProviderCatalogPage: ...


class CredentialSource(Protocol[CredentialValueT]):
    async def acquire(
        self,
        handle: ProviderCredentialHandle,
        *,
        provider: ProviderName,
        destination_sha256: Sha256,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> CredentialValueT: ...

    async def refresh(
        self,
        credential: CredentialValueT,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> CredentialValueT: ...

    async def release(self, credential: CredentialValueT) -> None: ...


class RequestCompiler(Protocol[CompiledRequestCovariantT]):
    def compile(
        self,
        request: CanonicalProviderRequest,
        model: ProviderModelCapabilities,
        context: ProviderContextPlan,
    ) -> CompiledRequestCovariantT: ...


class StreamDecoder(Protocol[WireEventContravariantT]):
    def decode(
        self,
        event: WireEventContravariantT,
    ) -> ProviderStreamBatch: ...


class ContextCapability(Protocol):
    def plan(
        self,
        request: CanonicalProviderRequest,
        model: ProviderModelCapabilities,
    ) -> ProviderContextPlan: ...


class UsageAndPrice(Protocol):
    def estimate_cost_microusd(
        self,
        request: CanonicalProviderRequest,
        model: ProviderModelCapabilities,
        price: ProviderPriceRecord,
    ) -> int: ...

    def record_usage(
        self,
        *,
        route_id: RouteId,
        provider_request_sha256: Sha256,
        model: ProviderModelCapabilities,
        price: ProviderPriceRecord,
        usage: ProviderTokenUsage,
        estimated_cost_microusd: int,
        recorded_at: datetime,
    ) -> ProviderUsageMetadata: ...


class DataPolicy(Protocol):
    def evaluate(
        self,
        request: CanonicalProviderRequest,
        model: ProviderModelCapabilities,
        policy: ProviderDataPolicy,
        region: Region,
        *,
        decided_at: datetime,
    ) -> ProviderDataPolicyDecision: ...
