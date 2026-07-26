"""Deterministic no-network components for the provider dispatch drill."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from datetime import datetime, timedelta

from app.services.harness.journal import SQLiteProviderCostLedger
from app.services.harness.protocol import (
    DataClassification,
    ProviderCostLimits,
    ProviderRetryBudget,
)
from app.services.harness.providers import (
    AuthorizedEgressTarget,
    CredentialLease,
    GatewayProviderAttemptExecutor,
    ProviderDispatchCoordinator,
    ProviderDispatchError,
    ProviderDispatchRequest,
    ProviderEgressGateway,
    ProviderEgressPolicy,
    ProviderEgressRequest,
    ProviderEgressResponse,
    provider_egress_request_sha256,
)

WORKSPACE_ID = "wsp_" + "1" * 32


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, milliseconds: int) -> None:
        self.now += timedelta(milliseconds=milliseconds)


class AdvancingRetryDelay:
    def __init__(self, clock: MutableClock) -> None:
        self._clock = clock
        self.delays_ms: list[int] = []

    async def wait(
        self,
        delay_ms: int,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> None:
        if cancellation.is_set():
            raise asyncio.CancelledError
        if self._clock() + timedelta(milliseconds=delay_ms) >= deadline_at:
            raise TimeoutError("provider retry delay exceeds deadline")
        self.delays_ms.append(delay_ms)
        self._clock.advance(delay_ms)


class ScenarioConnector:
    """Returns queued status codes or raises after a dispatch was attempted."""

    def __init__(
        self,
        scenarios: dict[str, tuple[int | None, ...]],
    ) -> None:
        self._scenarios = {
            request_id: deque(outcomes)
            for request_id, outcomes in scenarios.items()
        }
        self.calls: list[str] = []
        self.credential_present: list[bool] = []

    async def send(
        self,
        request: ProviderEgressRequest,
        target: AuthorizedEgressTarget,
        credential: CredentialLease | None,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
        max_response_bytes: int,
    ) -> ProviderEgressResponse:
        request_id = request.metadata.request_id
        self.calls.append(request_id)
        self.credential_present.append(credential is not None)
        outcomes = self._scenarios.get(request_id)
        if outcomes is None or not outcomes:
            raise AssertionError("provider drill has no queued outcome")
        status = outcomes.popleft()
        if status is None:
            raise RuntimeError("synthetic ambiguous connector outcome")
        return ProviderEgressResponse(
            status=status,
            headers=(),
            body=b'{"drill":true}',
        )


def call_counts(calls: list[str]) -> dict[str, int]:
    counts: defaultdict[str, int] = defaultdict(int)
    for request_id in calls:
        counts[request_id] += 1
    return dict(sorted(counts.items()))


def egress_request(
    request_id: str,
    provider: str,
    credential_handle: str,
    target_url: str,
    prompt_canary: str,
) -> ProviderEgressRequest:
    body = json.dumps(
        {"prompt": prompt_canary, "request_id": request_id},
        separators=(",", ":"),
    ).encode()
    return ProviderEgressRequest(
        request_id=request_id,
        provider=provider,
        credential_handle=credential_handle,
        target_url=target_url,
        classification=DataClassification.CONFIDENTIAL,
        content_type="application/json",
        safe_headers=(),
        body=body,
    )


async def dispatch_scenario(
    coordinator: ProviderDispatchCoordinator,
    gateway: ProviderEgressGateway,
    request: ProviderEgressRequest,
    policy: ProviderEgressPolicy,
    turn_id: str,
    clock: MutableClock,
    *,
    max_turn_cost: int,
) -> ProviderEgressResponse:
    request_hash = provider_egress_request_sha256(request)
    return await coordinator.dispatch(
        ProviderDispatchRequest(
            workspace_id=WORKSPACE_ID,
            turn_id=turn_id,
            request_id=request.metadata.request_id,
            provider_request_sha256=request_hash,
            estimated_attempt_cost_microusd=50,
            cost_limits=ProviderCostLimits(
                max_call_microusd=50,
                max_turn_microusd=max_turn_cost,
                max_workspace_microusd=500,
            ),
            retry_budget=ProviderRetryBudget(
                max_attempts=3,
                max_wall_time_ms=5_000,
                base_delay_ms=1,
                max_delay_ms=10,
            ),
            deadline_at=clock() + timedelta(seconds=10),
        ),
        GatewayProviderAttemptExecutor(
            gateway,
            request,
            policy,
            lambda response: 30,
            clock=clock,
        ),
        cancellation=asyncio.Event(),
    )


async def failed_dispatch_scenario(
    coordinator: ProviderDispatchCoordinator,
    gateway: ProviderEgressGateway,
    request: ProviderEgressRequest,
    policy: ProviderEgressPolicy,
    turn_id: str,
    clock: MutableClock,
    *,
    max_turn_cost: int,
) -> str:
    try:
        await dispatch_scenario(
            coordinator,
            gateway,
            request,
            policy,
            turn_id,
            clock,
            max_turn_cost=max_turn_cost,
        )
    except ProviderDispatchError as error:
        return error.code.value
    raise AssertionError("provider drill dispatch unexpectedly succeeded")


async def settled_cost(
    ledger: SQLiteProviderCostLedger,
    turn_id: str,
    clock: MutableClock,
) -> int:
    snapshot = await ledger.snapshot(
        WORKSPACE_ID,
        turn_id,
        observed_at=clock(),
    )
    if snapshot.active_reservations != 0:
        raise AssertionError("provider drill leaked an active reservation")
    return snapshot.turn_settled_microusd
