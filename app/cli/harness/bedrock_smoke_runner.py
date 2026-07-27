"""Explicit two-call Bedrock live smoke with signed durable spend limits."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from app.cli.harness.bedrock_smoke_attempt import (
    BedrockSmokeAttemptExecutor,
    BedrockSmokeCallEvidence,
    BedrockSmokeKind,
)
from app.cli.harness.bedrock_smoke_contracts import (
    AdmittedBedrockSmokeLaunch,
)
from app.cli.harness.bedrock_smoke_io import (
    BedrockSmokeResult,
    write_bedrock_smoke_result,
)
from app.cli.harness.bedrock_smoke_requests import (
    CompiledBedrockSmokeRequests,
    build_bedrock_smoke_requests,
)
from app.cli.harness.bedrock_smoke_runtime import (
    BedrockSmokeRuntime,
    load_bedrock_smoke_runtime,
)
from app.cli.harness.smoke_timing import smoke_latency_ms
from app.services.harness.journal import SQLiteProviderCostLedger
from app.services.harness.protocol import (
    ProviderCostLimits,
    ProviderRetryBudget,
)
from app.services.harness.providers import (
    BedrockBlockingCredentialBackend,
    BedrockConverseStreamDecoder,
    BedrockRuntimeClientFactory,
    BedrockTransportError,
    BedrockTransportErrorCode,
    Boto3BedrockRuntimeClientFactory,
    BotocoreNetworkCredentialBackend,
    BoundedBedrockConverseTransport,
    BoundedBedrockCredentialResolver,
    CancellableProviderRetryDelay,
    CompiledBedrockConverseStreamRequest,
    ProviderDispatchCoordinator,
    ProviderDispatchRequest,
)

Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]


async def run_bedrock_smoke(
    launch: AdmittedBedrockSmokeLaunch,
    *,
    environment: Mapping[bytes, bytes] | None = None,
    credential_backend: BedrockBlockingCredentialBackend | None = None,
    client_factory: BedrockRuntimeClientFactory | None = None,
    clock: Clock | None = None,
    monotonic_clock: MonotonicClock | None = None,
) -> BedrockSmokeResult:
    runtime_clock = clock or _utc_now
    runtime_monotonic = monotonic_clock or time.monotonic
    runtime = await load_bedrock_smoke_runtime(
        launch,
        environment=environment,
        observed_at=runtime_clock(),
    )
    deadline_at = runtime_clock() + timedelta(
        seconds=runtime.authorized.grant.timeout_seconds
    )
    requests = build_bedrock_smoke_requests(
        runtime.authorized,
        runtime.route,
        runtime.model,
        deadline_at=deadline_at,
    )
    backend = credential_backend or BotocoreNetworkCredentialBackend(
        runtime.route
    )
    resolver = BoundedBedrockCredentialResolver(
        backend,
        clock=runtime_clock,
        maximum_workers=1,
        maximum_pending=2,
    )
    transport = BoundedBedrockConverseTransport(
        client_factory or Boto3BedrockRuntimeClientFactory(),
        clock=runtime_clock,
        maximum_concurrent_streams=1,
    )
    ledger: SQLiteProviderCostLedger | None = None
    try:
        smoke_started_at = runtime_monotonic()
        cancellation_latency_ms = await _verify_cancellation(
            runtime,
            requests,
            resolver,
            transport,
            deadline_at,
            runtime_monotonic,
        )
        ledger = await SQLiteProviderCostLedger.open(
            launch.database_path,
            maximum_pending_operations=4,
        )
        text_evidence = await _dispatch_call(
            runtime,
            requests,
            requests.text,
            requests.text_request_id,
            requests.text_sha256,
            runtime.authorized.grant.text_cost_cap_microusd,
            BedrockSmokeKind.TEXT,
            resolver,
            transport,
            ledger,
            deadline_at,
            runtime_clock,
        )
        tool_evidence = await _dispatch_call(
            runtime,
            requests,
            requests.tool,
            requests.tool_request_id,
            requests.tool_sha256,
            runtime.authorized.grant.tool_cost_cap_microusd,
            BedrockSmokeKind.TOOL,
            resolver,
            transport,
            ledger,
            deadline_at,
            runtime_clock,
        )
        snapshot = await ledger.snapshot(
            requests.workspace_id,
            requests.turn_id,
            observed_at=runtime_clock(),
        )
        if (
            snapshot.active_reservations != 0
            or snapshot.workspace_settled_microusd
            != text_evidence.usage.cost_microusd
            + tool_evidence.usage.cost_microusd
            or snapshot.workspace_settled_microusd
            > runtime.authorized.grant.total_cost_cap_microusd
        ):
            raise ValueError("Bedrock smoke cost evidence is inconsistent")
        completed_at = runtime_clock()
        result = _result(
            runtime,
            requests,
            text_evidence,
            tool_evidence,
            snapshot.workspace_settled_microusd,
            smoke_latency_ms(smoke_started_at, runtime_monotonic()),
            cancellation_latency_ms,
            completed_at,
        )
    finally:
        await _close_runtime(ledger, transport, resolver)
    await write_bedrock_smoke_result(launch.result_path, result)
    return result


async def _verify_cancellation(
    runtime: BedrockSmokeRuntime,
    requests: CompiledBedrockSmokeRequests,
    resolver: BoundedBedrockCredentialResolver,
    transport: BoundedBedrockConverseTransport,
    deadline_at: datetime,
    monotonic_clock: MonotonicClock,
) -> int:
    started_at = monotonic_clock()
    credential = await resolver.resolve(
        runtime.identity,
        cancellation=asyncio.Event(),
        deadline_at=deadline_at,
    )
    cancellation = asyncio.Event()
    cancellation.set()
    decoder = BedrockConverseStreamDecoder(lambda *_counts: 0)
    try:
        async for _event in transport.stream(
            requests.text,
            runtime.route,
            credential,
            decoder,
            cancellation=cancellation,
            deadline_at=deadline_at,
        ):
            raise ValueError("cancelled Bedrock smoke emitted an event")
    except BedrockTransportError as error:
        if error.code is not BedrockTransportErrorCode.CANCELLED:
            raise
    views = credential.views()
    if any(bytes(view).strip(b"\x00") for view in views if view is not None):
        raise ValueError("cancelled Bedrock credential was not cleared")
    return smoke_latency_ms(started_at, monotonic_clock())


async def _dispatch_call(
    runtime: BedrockSmokeRuntime,
    requests: CompiledBedrockSmokeRequests,
    compiled_request: CompiledBedrockConverseStreamRequest,
    request_id: str,
    request_sha256: str,
    call_cap_microusd: int,
    kind: BedrockSmokeKind,
    resolver: BoundedBedrockCredentialResolver,
    transport: BoundedBedrockConverseTransport,
    ledger: SQLiteProviderCostLedger,
    deadline_at: datetime,
    clock: Clock,
) -> BedrockSmokeCallEvidence:
    total_cap = runtime.authorized.grant.total_cost_cap_microusd
    coordinator = ProviderDispatchCoordinator(
        ledger,
        CancellableProviderRetryDelay(),
        clock=clock,
    )
    executor = BedrockSmokeAttemptExecutor(
        compiled_request,
        runtime.route,
        runtime.identity,
        runtime.price,
        kind,
        resolver,
        transport,
    )
    return await coordinator.dispatch(
        ProviderDispatchRequest(
            workspace_id=requests.workspace_id,
            turn_id=requests.turn_id,
            request_id=request_id,
            provider_request_sha256=request_sha256,
            estimated_attempt_cost_microusd=call_cap_microusd,
            cost_limits=ProviderCostLimits(
                max_call_microusd=call_cap_microusd,
                max_turn_microusd=total_cap,
                max_workspace_microusd=total_cap,
            ),
            retry_budget=ProviderRetryBudget(
                max_attempts=1,
                max_wall_time_ms=(
                    runtime.authorized.grant.timeout_seconds * 1_000
                ),
                base_delay_ms=1,
                max_delay_ms=1,
            ),
            deadline_at=deadline_at,
        ),
        executor,
        cancellation=asyncio.Event(),
    )


def _result(
    runtime: BedrockSmokeRuntime,
    requests: CompiledBedrockSmokeRequests,
    text: BedrockSmokeCallEvidence,
    tool: BedrockSmokeCallEvidence,
    charged_cost_microusd: int,
    latency_ms: int,
    cancellation_latency_ms: int,
    completed_at: datetime,
) -> BedrockSmokeResult:
    return BedrockSmokeResult(
        model=runtime.route.model_id,
        region=runtime.route.region,
        authorization_id_sha256=hashlib.sha256(
            runtime.authorized.grant.authorization_id.encode()
        ).hexdigest(),
        destination_sha256=runtime.route.destination_sha256,
        text_request_sha256=requests.text_sha256,
        tool_request_sha256=requests.tool_sha256,
        text_provider_metadata_sha256=(
            text.metadata.provider_metadata_sha256
        ),
        tool_provider_metadata_sha256=(
            tool.metadata.provider_metadata_sha256
        ),
        input_tokens=text.usage.input_tokens + tool.usage.input_tokens,
        cached_input_tokens=(
            text.usage.cached_input_tokens + tool.usage.cached_input_tokens
        ),
        output_tokens=text.usage.output_tokens + tool.usage.output_tokens,
        reasoning_tokens=(
            text.usage.reasoning_tokens + tool.usage.reasoning_tokens
        ),
        latency_ms=latency_ms,
        cancellation_latency_ms=cancellation_latency_ms,
        charged_cost_microusd=charged_cost_microusd,
        signed_cost_cap_microusd=(
            runtime.authorized.grant.total_cost_cap_microusd
        ),
        completed_at=completed_at,
    )


async def _close_runtime(
    ledger: SQLiteProviderCostLedger | None,
    transport: BoundedBedrockConverseTransport,
    resolver: BoundedBedrockCredentialResolver,
) -> None:
    operations = [transport.close(), resolver.close()]
    if ledger is not None:
        operations.append(ledger.close())
    results = await asyncio.gather(*operations, return_exceptions=True)
    if any(isinstance(result, BaseException) for result in results):
        raise RuntimeError("Bedrock smoke runtime cleanup failed")


def _utc_now() -> datetime:
    return datetime.now(UTC)
