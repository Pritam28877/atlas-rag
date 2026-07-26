"""Independent mock implementations for every provider capability trait."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.services.harness.protocol import (
    CanonicalProviderRequest,
    ProviderCatalogPage,
    ProviderCatalogPageRequest,
    ProviderCatalogSummary,
    ProviderCompleted,
    ProviderContextFeature,
    ProviderContextPlan,
    ProviderCredentialHandle,
    ProviderDataPolicy,
    ProviderDataPolicyDecision,
    ProviderFinishReason,
    ProviderListPage,
    ProviderListPageRequest,
    ProviderModelCapabilities,
    ProviderPriceRecord,
    ProviderStreamBatch,
    ProviderTokenUsage,
    ProviderUsageMetadata,
)
from tests.harness_provider_capability_fixtures import NOW, model


class StaticCatalog:
    async def list_providers(
        self,
        request: ProviderListPageRequest,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> ProviderListPage:
        if cancellation.is_set():
            raise asyncio.CancelledError
        return ProviderListPage(
            providers=(
                ProviderCatalogSummary(
                    provider=model().provider,
                    catalog_snapshot_sha256=(
                        model().catalog_snapshot_sha256
                    ),
                    model_count=1,
                    observed_at=deadline_at - timedelta(seconds=1),
                ),
            ),
            has_more=False,
        )

    async def page(
        self,
        request: ProviderCatalogPageRequest,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> ProviderCatalogPage:
        if cancellation.is_set():
            raise asyncio.CancelledError
        return ProviderCatalogPage(
            provider=request.provider,
            catalog_snapshot_sha256=model().catalog_snapshot_sha256,
            models=(model(),),
            has_more=False,
            observed_at=deadline_at - timedelta(seconds=1),
        )


@dataclass(frozen=True)
class EphemeralCredential:
    handle_sha256: str
    generation: int


class EphemeralCredentialSource:
    def __init__(self) -> None:
        self.released: list[EphemeralCredential] = []

    async def acquire(
        self,
        handle: ProviderCredentialHandle,
        *,
        provider: str,
        destination_sha256: str,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> EphemeralCredential:
        if cancellation.is_set() or deadline_at <= NOW:
            raise asyncio.CancelledError
        digest_input = f"{handle}:{provider}:{destination_sha256}".encode()
        return EphemeralCredential(
            handle_sha256=hashlib.sha256(digest_input).hexdigest(),
            generation=1,
        )

    async def refresh(
        self,
        credential: EphemeralCredential,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> EphemeralCredential:
        if cancellation.is_set() or deadline_at <= NOW:
            raise asyncio.CancelledError
        return EphemeralCredential(
            handle_sha256=credential.handle_sha256,
            generation=credential.generation + 1,
        )

    async def release(self, credential: EphemeralCredential) -> None:
        self.released.append(credential)


class PromptCacheContext:
    def plan(
        self,
        request: CanonicalProviderRequest,
        model: ProviderModelCapabilities,
    ) -> ProviderContextPlan:
        request_digest = hashlib.sha256(
            request.model_dump_json().encode()
        ).hexdigest()
        return ProviderContextPlan(
            provider_request_sha256=request_digest,
            model_revision_sha256=model.model_revision_sha256,
            applied_features=(ProviderContextFeature.PROMPT_CACHE,),
            estimated_input_tokens=4,
            prompt_cache_key_sha256="8" * 64,
            reason="Configured prompt cache is compatible.",
        )


class ClassificationPolicy:
    def evaluate(
        self,
        request: CanonicalProviderRequest,
        model: ProviderModelCapabilities,
        policy: ProviderDataPolicy,
        region: str,
        *,
        decided_at: datetime,
    ) -> ProviderDataPolicyDecision:
        allowed = (
            request.classification in policy.accepted_classifications
            and region in policy.allowed_regions
            and region in model.available_regions
        )
        return ProviderDataPolicyDecision(
            route_id=request.route_id,
            policy_revision_sha256=policy.policy_revision_sha256,
            model_revision_sha256=model.model_revision_sha256,
            classification=request.classification,
            region=region,
            allowed=allowed,
            reason="All configured data-policy constraints matched.",
            decided_at=decided_at,
        )


@dataclass(frozen=True)
class CompiledRequest:
    request_sha256: str


class DigestCompiler:
    def compile(
        self,
        request: CanonicalProviderRequest,
        model: ProviderModelCapabilities,
        context: ProviderContextPlan,
    ) -> CompiledRequest:
        content = (
            request.model_dump_json()
            + model.model_revision_sha256
            + context.model_dump_json()
        )
        return CompiledRequest(
            request_sha256=hashlib.sha256(content.encode()).hexdigest()
        )


@dataclass(frozen=True)
class WireEvent:
    name: str


class RecordedTransport:
    async def stream(
        self,
        request: CompiledRequest,
        credential: EphemeralCredential,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> AsyncIterator[WireEvent]:
        if cancellation.is_set() or deadline_at <= NOW:
            raise asyncio.CancelledError
        if not request.request_sha256 or not credential.handle_sha256:
            raise RuntimeError("invalid injected capability")
        yield WireEvent(name="completed")


class RecordedDecoder:
    def decode(self, event: WireEvent) -> ProviderStreamBatch:
        if event.name != "completed":
            raise ValueError("unknown recorded wire event")
        return ProviderStreamBatch(
            events=(
                ProviderCompleted(
                    sequence=1,
                    finish_reason=ProviderFinishReason.STOP,
                ),
            )
        )


class FixedUsageAndPrice:
    def estimate_cost_microusd(
        self,
        request: CanonicalProviderRequest,
        model: ProviderModelCapabilities,
        price: ProviderPriceRecord,
    ) -> int:
        del model
        return (
            request.reserved_output_tokens
            * price.output_microusd_per_million_tokens
            // 1_000_000
        )

    def record_usage(
        self,
        *,
        route_id: str,
        provider_request_sha256: str,
        model: ProviderModelCapabilities,
        price: ProviderPriceRecord,
        usage: ProviderTokenUsage,
        estimated_cost_microusd: int,
        recorded_at: datetime,
    ) -> ProviderUsageMetadata:
        return ProviderUsageMetadata(
            route_id=route_id,
            provider_request_sha256=provider_request_sha256,
            model_revision_sha256=model.model_revision_sha256,
            price_version_sha256=price.price_version_sha256,
            usage=usage,
            estimated_cost_microusd=estimated_cost_microusd,
            recorded_at=recorded_at,
        )
