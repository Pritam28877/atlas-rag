"""One-call, explicitly gated local-compatible live smoke."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from app.cli.harness.adapter_smoke_contracts import AuthorizedAdapterSmoke
from app.cli.harness.adapter_smoke_io import (
    MAXIMUM_ADAPTER_SMOKE_EVENTS,
    MAXIMUM_ADAPTER_SMOKE_OUTPUT_BYTES,
    AdapterSmokeResult,
    build_adapter_smoke_result,
    write_adapter_smoke_result,
)
from app.cli.harness.adapter_smoke_runtime import (
    AdapterSmokeCostTracker,
    require_adapter_smoke_gate,
    select_adapter_smoke_route,
    smoke_binding_sha256,
    smoke_identifier,
    verify_pre_cancelled_adapter_stream,
)
from app.cli.harness.local_adapter_credentials import (
    acquire_local_adapter_credential,
)
from app.cli.harness.provider_smoke_builders import (
    build_canonical_smoke_request,
    build_smoke_context,
)
from app.cli.harness.provider_smoke_io import (
    read_private_smoke_input,
    validate_private_smoke_input,
    validate_provider_smoke_output_path,
)
from app.cli.harness.smoke_timing import smoke_latency_ms
from app.services.harness.journal import SQLiteProviderCostLedger
from app.services.harness.protocol import ProviderTextDelta
from app.services.harness.providers import (
    ConfiguredCredentialBroker,
    SystemProviderAddressResolver,
    load_provider_configuration,
)
from app.services.harness.providers.credential_material import CredentialLease
from app.services.harness.providers.local_compatible_capabilities import (
    LocalCompatibleProbe,
    decide_local_compatible_request,
)
from app.services.harness.providers.local_compatible_compiler import (
    LocalCompatibleResponsesCompiler,
)
from app.services.harness.providers.local_compatible_httpcore import (
    LocalCompatibleHttpCoreConnector,
)
from app.services.harness.providers.local_compatible_identity import (
    LocalCompatibleIdentityReference,
)
from app.services.harness.providers.local_compatible_policy import (
    LocalCompatibleRoutePolicy,
    authorize_local_compatible_route,
)
from app.services.harness.providers.local_compatible_stream_transport import (
    BoundedLocalCompatibleResponsesTransport,
    LocalCompatibleTransportErrorCode,
    LocalStreamingConnector,
)
from app.services.harness.providers.openai_decoder import OpenAIResponsesDecoder

Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]
MAXIMUM_ADAPTER_SMOKE_INPUT_BYTES = 64 * 1024


async def run_local_adapter_smoke(
    authorized: AuthorizedAdapterSmoke,
    *,
    connector: LocalStreamingConnector | None = None,
    environment: Mapping[bytes, bytes] | None = None,
    clock: Clock | None = None,
    monotonic_clock: MonotonicClock | None = None,
) -> AdapterSmokeResult:
    if authorized.provider != "local-compatible":
        raise ValueError("local smoke received another provider")
    runtime_clock = clock or _utc_now
    runtime_monotonic = monotonic_clock or time.monotonic
    require_adapter_smoke_gate(authorized, environment)
    await _validate_paths(authorized)
    loaded = await load_provider_configuration(
        authorized.configuration_path
    )
    configured_route, model = select_adapter_smoke_route(
        authorized,
        loaded,
    )
    policy, identity, probe = await _load_local_contracts(authorized)
    route = authorize_local_compatible_route(
        loaded,
        configured_route,
        policy,
        identity,
    )
    now = runtime_clock()
    deadline_at = now + timedelta(seconds=authorized.timeout_seconds)
    request_id = smoke_identifier("req")
    turn_id = smoke_identifier("trn")
    canonical_request = build_canonical_smoke_request(
        configured_route,
        request_id=request_id,
        turn_id=turn_id,
        deadline_at=deadline_at,
        max_output_tokens=authorized.max_output_tokens,
    )
    context = build_smoke_context(canonical_request, model)
    decision = decide_local_compatible_request(
        canonical_request,
        context,
        route,
        probe,
        decided_at=now,
    )
    compiled = LocalCompatibleResponsesCompiler().compile(
        canonical_request,
        model,
        context,
        decision,
    )
    request_sha256 = hashlib.sha256(
        compiled.model_dump_json(exclude_none=True).encode()
    ).hexdigest()
    ledger = await SQLiteProviderCostLedger.open(
        authorized.database_path,
        maximum_pending_operations=4,
    )
    tracker = AdapterSmokeCostTracker(
        ledger,
        workspace_id=smoke_identifier("wsp"),
        turn_id=turn_id,
        request_id=request_id,
        provider_request_sha256=request_sha256,
        cost_cap_microusd=0,
    )
    broker: ConfiguredCredentialBroker | None = None
    credential: CredentialLease | None = None
    try:
        await tracker.reserve(runtime_clock())
        broker, credential = await acquire_local_adapter_credential(
            loaded=loaded,
            route=route,
            identity=identity,
            credential_environment_variable=(
                authorized.credential_environment_variable
            ),
            environment=environment,
            clock=runtime_clock,
            deadline_at=deadline_at,
            lease_ttl_seconds=authorized.timeout_seconds,
        )
        selected_connector = connector or LocalCompatibleHttpCoreConnector(
            SystemProviderAddressResolver(),
            clock=runtime_clock,
        )
        transport = BoundedLocalCompatibleResponsesTransport(
            selected_connector,
            clock=runtime_clock,
            maximum_concurrent_streams=1,
        )
        pre_cancelled = asyncio.Event()
        pre_cancelled.set()
        cancellation_latency_ms = (
            await verify_pre_cancelled_adapter_stream(
                transport.stream(
                    canonical_request,
                    compiled,
                    route,
                    credential,
                    OpenAIResponsesDecoder(_zero_cost),
                    cancellation=pre_cancelled,
                    deadline_at=deadline_at,
                ),
                expected_error_code=(
                    LocalCompatibleTransportErrorCode.CANCELLED
                ),
                monotonic_clock=runtime_monotonic,
            )
        )
        events = []
        output_bytes = 0
        provider_started_at = runtime_monotonic()
        async with asyncio.timeout(authorized.timeout_seconds):
            async for event in transport.stream(
                canonical_request,
                compiled,
                route,
                credential,
                OpenAIResponsesDecoder(_zero_cost),
                cancellation=asyncio.Event(),
                deadline_at=deadline_at,
            ):
                events.append(event)
                if isinstance(event, ProviderTextDelta):
                    output_bytes += len(event.text.encode())
                if (
                    len(events) > MAXIMUM_ADAPTER_SMOKE_EVENTS
                    or output_bytes > MAXIMUM_ADAPTER_SMOKE_OUTPUT_BYTES
                ):
                    raise ValueError("local smoke response exceeded its bound")
        provider_completed_at = runtime_clock()
        await transport.close()
        await tracker.settle(runtime_clock())
        result = build_adapter_smoke_result(
            provider="local-compatible",
            model=authorized.model,
            request_id=request_id,
            events=events,
            route_binding_sha256=smoke_binding_sha256(
                policy.model_dump_json().encode(),
                identity.model_dump_json().encode(),
                probe.model_dump_json().encode(),
            ),
            latency_ms=smoke_latency_ms(
                provider_started_at,
                runtime_monotonic(),
            ),
            cancellation_latency_ms=cancellation_latency_ms,
            charged_cost_microusd=0,
            completed_at=provider_completed_at,
        )
    except BaseException:
        await tracker.release(runtime_clock())
        raise
    finally:
        try:
            if broker is not None and credential is not None:
                await broker.release(credential)
        finally:
            await ledger.close()
    await write_adapter_smoke_result(authorized.result_path, result)
    return result


async def _validate_paths(authorized: AuthorizedAdapterSmoke) -> None:
    inputs = (
        authorized.configuration_path,
        authorized.route_policy_path,
        authorized.identity_path,
        authorized.capability_evidence_path,
    )
    if any(path is None for path in inputs):
        raise ValueError("local smoke input path is missing")
    for path in inputs:
        if path is not None:
            await validate_private_smoke_input(path)
    await validate_provider_smoke_output_path(authorized.database_path)
    await validate_provider_smoke_output_path(authorized.result_path)


async def _load_local_contracts(
    authorized: AuthorizedAdapterSmoke,
) -> tuple[
    LocalCompatibleRoutePolicy,
    LocalCompatibleIdentityReference,
    LocalCompatibleProbe,
]:
    evidence_path = authorized.capability_evidence_path
    if evidence_path is None:
        raise ValueError("local capability evidence path is missing")
    contents = await asyncio.gather(
        read_private_smoke_input(
            authorized.route_policy_path,
            maximum_bytes=MAXIMUM_ADAPTER_SMOKE_INPUT_BYTES,
        ),
        read_private_smoke_input(
            authorized.identity_path,
            maximum_bytes=MAXIMUM_ADAPTER_SMOKE_INPUT_BYTES,
        ),
        read_private_smoke_input(
            evidence_path,
            maximum_bytes=MAXIMUM_ADAPTER_SMOKE_INPUT_BYTES,
        ),
    )
    return (
        LocalCompatibleRoutePolicy.model_validate_json(contents[0]),
        LocalCompatibleIdentityReference.model_validate_json(contents[1]),
        LocalCompatibleProbe.model_validate_json(contents[2]),
    )


def _zero_cost(
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
) -> int:
    del input_tokens, cached_tokens, output_tokens, reasoning_tokens
    return 0


def _utc_now() -> datetime:
    return datetime.now(UTC)
