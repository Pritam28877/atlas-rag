"""One-call, explicitly gated regional Vertex live smoke."""

from __future__ import annotations

import asyncio
import hashlib
import json
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
from app.services.harness.journal import SQLiteProviderCostLedger
from app.services.harness.protocol import ProviderTextDelta
from app.services.harness.providers import (
    ConfiguredCredentialBroker,
    SystemProviderAddressResolver,
    load_provider_configuration,
)
from app.services.harness.providers.credential_material import CredentialLease
from app.services.harness.providers.vertex_compiler import (
    VertexGenerateContentCompiler,
)
from app.services.harness.providers.vertex_credentials import (
    VertexCredentialBackend,
    VertexCredentialLoader,
)
from app.services.harness.providers.vertex_decoder import (
    VertexGenerateContentDecoder,
)
from app.services.harness.providers.vertex_httpcore import VertexHttpCoreConnector
from app.services.harness.providers.vertex_identity import (
    VertexIdentityReference,
)
from app.services.harness.providers.vertex_policy import (
    VertexRoutePolicy,
    authorize_vertex_route,
)
from app.services.harness.providers.vertex_stream_transport import (
    BoundedVertexGenerateContentTransport,
    VertexStreamingConnector,
)

Clock = Callable[[], datetime]
MAXIMUM_ADAPTER_SMOKE_INPUT_BYTES = 64 * 1024


async def run_vertex_adapter_smoke(
    authorized: AuthorizedAdapterSmoke,
    *,
    connector: VertexStreamingConnector | None = None,
    credential_loader: VertexCredentialLoader | None = None,
    environment: Mapping[bytes, bytes] | None = None,
    clock: Clock | None = None,
) -> AdapterSmokeResult:
    if authorized.provider != "vertex":
        raise ValueError("Vertex smoke received another provider")
    runtime_clock = clock or _utc_now
    require_adapter_smoke_gate(authorized, environment)
    await _validate_paths(authorized)
    loaded = await load_provider_configuration(
        authorized.configuration_path
    )
    configured_route, model = select_adapter_smoke_route(
        authorized,
        loaded,
    )
    policy, identity = await _load_vertex_contracts(authorized)
    if (
        authorized.disposable_project_id != policy.project_id
        or identity.quota_project_id != policy.project_id
    ):
        raise ValueError("Vertex smoke disposable project is unbound")
    route = authorize_vertex_route(
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
    compiled = VertexGenerateContentCompiler().compile(
        canonical_request,
        model,
        context,
    )
    request_sha256 = hashlib.sha256(
        json.dumps(
            compiled.to_wire(),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
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
        cost_cap_microusd=authorized.cost_cap_microusd,
    )
    broker = ConfiguredCredentialBroker(
        loaded,
        VertexCredentialBackend(
            (identity,),
            clock=runtime_clock,
            loader=credential_loader,
        ),
        clock=runtime_clock,
        maximum_active_leases=1,
    )
    credential: CredentialLease | None = None
    provider_call_started = False
    try:
        await tracker.reserve(runtime_clock())
        credential = await broker.acquire(
            route.credential_handle,
            provider="vertex",
            destination_sha256=route.destination_sha256,
            cancellation=asyncio.Event(),
            deadline_at=deadline_at,
        )
        selected_connector = connector or VertexHttpCoreConnector(
            SystemProviderAddressResolver(),
            clock=runtime_clock,
        )
        transport = BoundedVertexGenerateContentTransport(
            selected_connector,
            clock=runtime_clock,
            maximum_concurrent_streams=1,
        )
        events = []
        output_bytes = 0
        provider_call_started = True
        async with asyncio.timeout(authorized.timeout_seconds):
            async for event in transport.stream(
                canonical_request,
                compiled,
                route,
                credential,
                VertexGenerateContentDecoder(
                    _fixed_cost(authorized.cost_cap_microusd)
                ),
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
                    raise ValueError("Vertex smoke response exceeded its bound")
        await transport.close()
        await tracker.settle(runtime_clock())
        result = build_adapter_smoke_result(
            provider="vertex",
            model=authorized.model,
            request_id=request_id,
            events=events,
            route_binding_sha256=smoke_binding_sha256(
                policy.model_dump_json().encode(),
                identity.model_dump_json().encode(),
            ),
            charged_cost_microusd=authorized.cost_cap_microusd,
            completed_at=runtime_clock(),
        )
    except BaseException:
        if tracker.active:
            if provider_call_started:
                await tracker.settle(runtime_clock())
            else:
                await tracker.release(runtime_clock())
        raise
    finally:
        try:
            if credential is not None:
                await broker.release(credential)
        finally:
            await ledger.close()
    await write_adapter_smoke_result(authorized.result_path, result)
    return result


async def _validate_paths(authorized: AuthorizedAdapterSmoke) -> None:
    if authorized.capability_evidence_path is not None:
        raise ValueError("Vertex smoke capability evidence is unexpected")
    for path in (
        authorized.configuration_path,
        authorized.route_policy_path,
        authorized.identity_path,
    ):
        await validate_private_smoke_input(path)
    await validate_provider_smoke_output_path(authorized.database_path)
    await validate_provider_smoke_output_path(authorized.result_path)


async def _load_vertex_contracts(
    authorized: AuthorizedAdapterSmoke,
) -> tuple[VertexRoutePolicy, VertexIdentityReference]:
    contents = await asyncio.gather(
        read_private_smoke_input(
            authorized.route_policy_path,
            maximum_bytes=MAXIMUM_ADAPTER_SMOKE_INPUT_BYTES,
        ),
        read_private_smoke_input(
            authorized.identity_path,
            maximum_bytes=MAXIMUM_ADAPTER_SMOKE_INPUT_BYTES,
        ),
    )
    return (
        VertexRoutePolicy.model_validate_json(contents[0]),
        VertexIdentityReference.model_validate_json(contents[1]),
    )


def _fixed_cost(
    cost_microusd: int,
) -> Callable[[int, int, int, int], int]:
    def calculate(
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
    ) -> int:
        del input_tokens, cached_tokens, output_tokens, reasoning_tokens
        return cost_microusd

    return calculate


def _utc_now() -> datetime:
    return datetime.now(UTC)
