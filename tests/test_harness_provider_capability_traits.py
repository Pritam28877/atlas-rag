import asyncio
from datetime import timedelta

from app.services.harness.protocol import (
    ContextCapability,
    CredentialSource,
    DataPolicy,
    InferenceTransport,
    ModelCatalog,
    ProviderCatalogPage,
    ProviderCatalogPageRequest,
    ProviderContextFeature,
    ProviderContextPlan,
    ProviderDataPolicyDecision,
    ProviderStreamBatch,
    ProviderTokenUsage,
    ProviderUsageMetadata,
    RequestCompiler,
    StreamDecoder,
    UsageAndPrice,
)
from tests.harness_provider_capability_fixtures import (
    NOW,
    model,
    policy,
    price,
    request,
)
from tests.harness_provider_capability_mocks import (
    ClassificationPolicy,
    CompiledRequest,
    DigestCompiler,
    EphemeralCredential,
    EphemeralCredentialSource,
    FixedUsageAndPrice,
    PromptCacheContext,
    RecordedDecoder,
    RecordedTransport,
    StaticCatalog,
    WireEvent,
)


async def catalog_page(capability: ModelCatalog) -> ProviderCatalogPage:
    return await capability.page(
        ProviderCatalogPageRequest(provider="configured-provider"),
        cancellation=asyncio.Event(),
        deadline_at=NOW + timedelta(seconds=5),
    )


async def credential_lifecycle(
    capability: CredentialSource[EphemeralCredential],
) -> tuple[EphemeralCredential, EphemeralCredential]:
    acquired = await capability.acquire(
        "pcr_" + "9" * 32,
        provider="configured-provider",
        destination_sha256="6" * 64,
        cancellation=asyncio.Event(),
        deadline_at=NOW + timedelta(seconds=5),
    )
    refreshed = await capability.refresh(
        acquired,
        cancellation=asyncio.Event(),
        deadline_at=NOW + timedelta(seconds=5),
    )
    await capability.release(refreshed)
    return acquired, refreshed


def context_plan(capability: ContextCapability) -> ProviderContextPlan:
    return capability.plan(request(), model())


def data_decision(capability: DataPolicy) -> ProviderDataPolicyDecision:
    return capability.evaluate(
        request(),
        model(),
        policy(),
        "us-east-1",
        decided_at=NOW,
    )


def compiled_request(
    capability: RequestCompiler[CompiledRequest],
) -> CompiledRequest:
    context = context_plan(PromptCacheContext())
    return capability.compile(request(), model(), context)


async def wire_events(
    capability: InferenceTransport[
        CompiledRequest,
        EphemeralCredential,
        WireEvent,
    ],
    compiled: CompiledRequest,
    credential: EphemeralCredential,
) -> tuple[WireEvent, ...]:
    events: list[WireEvent] = []
    async for event in capability.stream(
        compiled,
        credential,
        cancellation=asyncio.Event(),
        deadline_at=NOW + timedelta(seconds=5),
    ):
        events.append(event)
    return tuple(events)


def decode(
    capability: StreamDecoder[WireEvent],
    event: WireEvent,
) -> ProviderStreamBatch:
    return capability.decode(event)


def usage_metadata(
    capability: UsageAndPrice,
    compiled: CompiledRequest,
) -> ProviderUsageMetadata:
    estimated_cost = capability.estimate_cost_microusd(
        request(),
        model(),
        price(),
    )
    return capability.record_usage(
        route_id=request().route_id,
        provider_request_sha256=compiled.request_sha256,
        model=model(),
        price=price(),
        usage=ProviderTokenUsage(
            input_tokens=4,
            cached_input_tokens=4,
            output_tokens=1,
            reasoning_tokens=0,
            cost_microusd=3,
        ),
        estimated_cost_microusd=estimated_cost,
        recorded_at=NOW,
    )


def test_capabilities_are_independently_substitutable() -> None:
    async def scenario() -> None:
        catalog = await catalog_page(StaticCatalog())
        credential_source = EphemeralCredentialSource()
        acquired, refreshed = await credential_lifecycle(credential_source)
        context = context_plan(PromptCacheContext())
        decision = data_decision(ClassificationPolicy())
        compiled = compiled_request(DigestCompiler())
        events = await wire_events(
            RecordedTransport(),
            compiled,
            refreshed,
        )
        batch = decode(RecordedDecoder(), events[0])
        usage = usage_metadata(FixedUsageAndPrice(), compiled)

        assert catalog.models == (model(),)
        assert refreshed.generation == acquired.generation + 1
        assert credential_source.released == [refreshed]
        assert context.applied_features == (
            ProviderContextFeature.PROMPT_CACHE,
        )
        assert decision.allowed
        assert batch.events[0].kind.value == "completed"
        assert usage.provider_request_sha256 == compiled.request_sha256

    asyncio.run(scenario())
