"""Explicit one-attempt live provider smoke composition."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta

from app.cli.harness.provider_smoke_builders import (
    build_canonical_smoke_request,
    build_smoke_context,
    build_smoke_egress,
    select_smoke_route,
)
from app.cli.harness.provider_smoke_contracts import AuthorizedProviderSmoke
from app.cli.harness.provider_smoke_decode import (
    decode_provider_smoke_response,
)
from app.cli.harness.provider_smoke_io import (
    ProviderSmokeResult,
    load_openrouter_smoke_policy,
    response_body_sha256,
    validate_private_smoke_input,
    validate_provider_smoke_output_path,
    write_provider_smoke_result,
)
from app.services.harness.journal import (
    GlobalJournalReadRequest,
    JournalProviderEgressAuditSink,
    SQLiteEventJournal,
    SQLiteProviderCostLedger,
)
from app.services.harness.protocol import (
    CanonicalProviderRequest,
    ProviderContextPlan,
    ProviderCostLimits,
    ProviderCredentialHandle,
    ProviderModelCapabilities,
    ProviderRetryBudget,
)
from app.services.harness.providers import (
    BearerProviderCredentialEncoder,
    CancellableProviderRetryDelay,
    CompiledOpenAIResponsesRequest,
    CompiledOpenRouterResponsesRequest,
    ConfiguredCredentialBroker,
    DeterministicProviderPayloadInspector,
    EnvironmentCredentialBackend,
    EnvironmentCredentialReference,
    GatewayProviderAttemptExecutor,
    HttpCoreEgressConnector,
    LoadedProviderConfiguration,
    OpenAIResponsesCompiler,
    OpenRouterResponsesCompiler,
    ProviderDispatchCoordinator,
    ProviderDispatchRequest,
    ProviderEgressConnector,
    ProviderEgressGateway,
    ProviderEgressPolicy,
    ProviderEgressRequest,
    ProviderEgressResponse,
    ProviderPayloadInspectionPolicy,
    SafeEgressHeader,
    SystemProviderAddressResolver,
    load_provider_configuration,
    provider_egress_request_sha256,
)

AddressLookup = Callable[
    [str, int],
    Awaitable[tuple[str, ...]],
]
Clock = Callable[[], datetime]


async def run_provider_smoke(
    authorized: AuthorizedProviderSmoke,
    *,
    connector: ProviderEgressConnector | None = None,
    lookup: AddressLookup | None = None,
    environment: Mapping[bytes, bytes] | None = None,
    clock: Clock | None = None,
) -> ProviderSmokeResult:
    runtime_clock = clock or _utc_now
    await validate_private_smoke_input(authorized.config_path)
    await validate_provider_smoke_output_path(authorized.result_path)
    loaded = await load_provider_configuration(authorized.config_path)
    route, model = select_smoke_route(authorized, loaded)
    deadline_at = runtime_clock() + timedelta(seconds=authorized.timeout_seconds)
    request_id = _identifier("req")
    turn_id = _identifier("trn")
    workspace_id = _identifier("wsp")
    principal_id = _identifier("prn")
    canonical_request = build_canonical_smoke_request(
        route,
        request_id=request_id,
        turn_id=turn_id,
        deadline_at=deadline_at,
        max_output_tokens=authorized.max_output_tokens,
    )
    context = build_smoke_context(canonical_request, model)
    compiled, safe_headers = await _compile_request(
        authorized,
        canonical_request,
        model,
        context,
    )
    body = compiled.model_dump_json(exclude_none=True).encode()
    egress_request, egress_policy = build_smoke_egress(
        authorized,
        route,
        request_id=request_id,
        body=body,
        safe_headers=safe_headers,
    )
    provider_request_sha256 = provider_egress_request_sha256(egress_request)
    journal: SQLiteEventJournal | None = None
    cost_ledger: SQLiteProviderCostLedger | None = None
    try:
        journal = await SQLiteEventJournal.open(
            authorized.database_path,
            maximum_pending_operations=8,
            clock=runtime_clock,
        )
        cost_ledger = await SQLiteProviderCostLedger.open(
            authorized.database_path,
            maximum_pending_operations=8,
        )
        broker = _credential_broker(
            authorized,
            loaded,
            route.credential_handle,
            runtime_clock,
            environment,
        )
        gateway = ProviderEgressGateway(
            credentials=broker,
            resolver=SystemProviderAddressResolver(lookup=lookup),
            inspector=DeterministicProviderPayloadInspector(
                ProviderPayloadInspectionPolicy(
                    policy_revision_sha256=route.policy_revision_sha256,
                    denied_body_sha256s=(),
                    maximum_scan_bytes=len(body),
                )
            ),
            connector=connector
            or HttpCoreEgressConnector(BearerProviderCredentialEncoder()),
            audit=JournalProviderEgressAuditSink(
                journal,
                workspace_id=workspace_id,
                actor_principal_id=principal_id,
            ),
            clock=runtime_clock,
        )
        response = await _dispatch_once(
            authorized,
            egress_request,
            egress_policy,
            provider_request_sha256,
            workspace_id,
            turn_id,
            request_id,
            deadline_at,
            gateway,
            cost_ledger,
            runtime_clock,
        )
        stream = decode_provider_smoke_response(
            authorized.provider,
            response.body(),
            charged_cost_microusd=authorized.cost_cap_microusd,
        )
        snapshot = await cost_ledger.snapshot(
            workspace_id,
            turn_id,
            observed_at=runtime_clock(),
        )
        audit_page = await journal.read_global(
            GlobalJournalReadRequest(
                workspace_id=workspace_id,
                after_journal_sequence=0,
                limit=64,
            )
        )
        active_leases = await broker.active_leases()
        _validate_runtime_evidence(
            authorized,
            snapshot.workspace_settled_microusd,
            snapshot.active_reservations,
            len(audit_page.events),
            audit_page.has_more,
            active_leases,
            stream.routing_metadata_sha256,
        )
        result = ProviderSmokeResult(
            provider=authorized.provider,
            model=authorized.model,
            request_id=request_id,
            response_status=response.status,
            response_body_sha256=response_body_sha256(response.body()),
            input_tokens=stream.usage.input_tokens,
            cached_input_tokens=stream.usage.cached_input_tokens,
            output_tokens=stream.usage.output_tokens,
            reasoning_tokens=stream.usage.reasoning_tokens,
            charged_cost_microusd=authorized.cost_cap_microusd,
            routing_metadata_sha256=stream.routing_metadata_sha256,
            audit_events=len(audit_page.events),
            active_credential_leases=active_leases,
            completed_at=runtime_clock(),
        )
    finally:
        await _close_runtime_stores(cost_ledger, journal)
    await write_provider_smoke_result(authorized.result_path, result)
    return result


async def _compile_request(
    authorized: AuthorizedProviderSmoke,
    canonical_request: CanonicalProviderRequest,
    model: ProviderModelCapabilities,
    context: ProviderContextPlan,
) -> tuple[
    CompiledOpenAIResponsesRequest | CompiledOpenRouterResponsesRequest,
    tuple[SafeEgressHeader, ...],
]:
    headers = [SafeEgressHeader(name="accept", value="text/event-stream")]
    compiled_request: (
        CompiledOpenAIResponsesRequest | CompiledOpenRouterResponsesRequest
    )
    if authorized.provider == "openrouter":
        policy_path = authorized.openrouter_policy_path
        if policy_path is None:
            raise ValueError("OpenRouter smoke policy is missing")
        policy = await load_openrouter_smoke_policy(policy_path)
        compiler = OpenRouterResponsesCompiler(policy)
        headers.append(compiler.response_metadata_header())
        compiled_request = compiler.compile(
            canonical_request,
            model,
            context,
        )
    else:
        compiled_request = OpenAIResponsesCompiler().compile(
            canonical_request,
            model,
            context,
        )
    return compiled_request, tuple(headers)


def _credential_broker(
    authorized: AuthorizedProviderSmoke,
    loaded: LoadedProviderConfiguration,
    credential_handle: ProviderCredentialHandle,
    clock: Clock,
    environment: Mapping[bytes, bytes] | None,
) -> ConfiguredCredentialBroker:
    backend = EnvironmentCredentialBackend(
        (
            EnvironmentCredentialReference(
                handle=credential_handle,
                environment_variable=authorized.environment_variable,
                lease_ttl_seconds=authorized.timeout_seconds,
            ),
        ),
        development_mode=True,
        clock=clock,
        environment=environment,
    )
    return ConfiguredCredentialBroker(loaded, backend, clock=clock)


async def _dispatch_once(
    authorized: AuthorizedProviderSmoke,
    request: ProviderEgressRequest,
    policy: ProviderEgressPolicy,
    provider_request_sha256: str,
    workspace_id: str,
    turn_id: str,
    request_id: str,
    deadline_at: datetime,
    gateway: ProviderEgressGateway,
    cost_ledger: SQLiteProviderCostLedger,
    clock: Clock,
) -> ProviderEgressResponse:
    def reserved_cost(_response: ProviderEgressResponse) -> int:
        return authorized.cost_cap_microusd

    executor = GatewayProviderAttemptExecutor(
        gateway,
        request,
        policy,
        reserved_cost,
        clock=clock,
    )
    cap = authorized.cost_cap_microusd
    coordinator = ProviderDispatchCoordinator(
        cost_ledger,
        CancellableProviderRetryDelay(),
        clock=clock,
    )
    return await coordinator.dispatch(
        ProviderDispatchRequest(
            workspace_id=workspace_id,
            turn_id=turn_id,
            request_id=request_id,
            provider_request_sha256=provider_request_sha256,
            estimated_attempt_cost_microusd=cap,
            cost_limits=ProviderCostLimits(
                max_call_microusd=cap,
                max_turn_microusd=cap,
                max_workspace_microusd=cap,
            ),
            retry_budget=ProviderRetryBudget(
                max_attempts=1,
                max_wall_time_ms=authorized.timeout_seconds * 1_000,
                base_delay_ms=1,
                max_delay_ms=1,
            ),
            deadline_at=deadline_at,
        ),
        executor,
        cancellation=asyncio.Event(),
    )


def _validate_runtime_evidence(
    authorized: AuthorizedProviderSmoke,
    settled_cost: int,
    active_reservations: int,
    audit_events: int,
    audit_has_more: bool,
    active_leases: int,
    routing_metadata_sha256: str | None,
) -> None:
    routing_metadata_is_valid = (
        routing_metadata_sha256 is not None
        if authorized.provider == "openrouter"
        else routing_metadata_sha256 is None
    )
    if (
        settled_cost != authorized.cost_cap_microusd
        or active_reservations != 0
        or audit_events != 2
        or audit_has_more
        or active_leases != 0
        or not routing_metadata_is_valid
    ):
        raise ValueError("provider smoke runtime evidence is inconsistent")


async def _close_runtime_stores(
    cost_ledger: SQLiteProviderCostLedger | None,
    journal: SQLiteEventJournal | None,
) -> None:
    close_operations = []
    if cost_ledger is not None:
        close_operations.append(cost_ledger.close())
    if journal is not None:
        close_operations.append(journal.close())
    results = await asyncio.gather(
        *close_operations,
        return_exceptions=True,
    )
    if any(isinstance(result, BaseException) for result in results):
        raise RuntimeError("provider smoke store cleanup failed")


def _identifier(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def _utc_now() -> datetime:
    return datetime.now(UTC)
