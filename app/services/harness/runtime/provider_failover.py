"""Policy-preserving cross-provider failover planning."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.services.harness.protocol import (
    MAXIMUM_PROVIDER_DECISION_ROUTES,
    ProviderFailureClass,
    ProviderRetryDisposition,
    ProviderRouteDecisionRecord,
    ProviderTransportFailure,
    RouteId,
    Sha256,
)
from app.services.harness.runtime.provider_router import select_provider_route

MAXIMUM_FAILOVER_COST_MICROUSD = 10_000_000_000


class ProviderAttemptEffectState(StrEnum):
    PRE_SIDE_EFFECT = "pre_side_effect"
    SIDE_EFFECT_STARTED = "side_effect_started"


class ProviderFailoverDecisionCode(StrEnum):
    AMBIGUOUS = "ambiguous"
    FAILOVER = "failover"
    FAILURE_INELIGIBLE = "failure_ineligible"
    NO_ALTERNATIVE = "no_alternative"
    REQUEST_MISMATCH = "request_mismatch"
    ROUTE_MISMATCH = "route_mismatch"
    SIDE_EFFECT_STARTED = "side_effect_started"


@dataclass(frozen=True, slots=True)
class ProviderFailoverDecision:
    code: ProviderFailoverDecisionCode
    reason: str
    selected_route_id: RouteId | None
    eligible_route_ids: tuple[RouteId, ...]
    cost_rejected_route_ids: tuple[RouteId, ...]
    decided_at: datetime

    @property
    def failover(self) -> bool:
        return self.code is ProviderFailoverDecisionCode.FAILOVER


def plan_provider_failover(
    route_decision: ProviderRouteDecisionRecord,
    failure: ProviderTransportFailure,
    *,
    provider_request_sha256: Sha256,
    failed_route_id: RouteId,
    attempted_route_ids: Sequence[RouteId],
    effect_state: ProviderAttemptEffectState,
    remaining_cost_microusd: int,
    decided_at: datetime,
) -> ProviderFailoverDecision:
    """Choose an unattempted route without weakening the original policy."""

    attempted = _validated_attempts(attempted_route_ids)
    _validate_runtime(
        route_decision,
        failure,
        effect_state=effect_state,
        remaining_cost_microusd=remaining_cost_microusd,
        decided_at=decided_at,
    )
    if failure.provider_request_sha256 != provider_request_sha256:
        return _denied(
            ProviderFailoverDecisionCode.REQUEST_MISMATCH,
            "Provider failure does not match the canonical request.",
            decided_at,
        )
    eligible_route_ids = {
        route.route_id for route in route_decision.eligible_routes
    }
    if (
        failed_route_id not in eligible_route_ids
        or failed_route_id not in attempted
        or route_decision.selected_route_id not in attempted
    ):
        return _denied(
            ProviderFailoverDecisionCode.ROUTE_MISMATCH,
            "Failed route is not part of the attempted eligible route set.",
            decided_at,
        )
    if failure.ambiguous:
        return _denied(
            ProviderFailoverDecisionCode.AMBIGUOUS,
            "Ambiguous provider work cannot fail over.",
            decided_at,
        )
    if effect_state is ProviderAttemptEffectState.SIDE_EFFECT_STARTED:
        return _denied(
            ProviderFailoverDecisionCode.SIDE_EFFECT_STARTED,
            "Provider work that started a side effect cannot fail over.",
            decided_at,
        )
    if (
        failure.failure_class is not ProviderFailureClass.TRANSIENT
        or failure.retry_disposition
        is not ProviderRetryDisposition.ELIGIBLE
    ):
        return _denied(
            ProviderFailoverDecisionCode.FAILURE_INELIGIBLE,
            "Provider failure is not classified for failover.",
            decided_at,
        )

    unattempted_routes = tuple(
        route
        for route in route_decision.eligible_routes
        if route.route_id not in attempted
    )
    affordable_routes = tuple(
        route
        for route in unattempted_routes
        if route.estimated_cost_microusd <= remaining_cost_microusd
    )
    cost_rejected = tuple(
        sorted(
            route.route_id
            for route in unattempted_routes
            if route.estimated_cost_microusd > remaining_cost_microusd
        )
    )
    if not affordable_routes:
        return _denied(
            ProviderFailoverDecisionCode.NO_ALTERNATIVE,
            "No unattempted policy-eligible route fits the remaining cost.",
            decided_at,
            cost_rejected_route_ids=cost_rejected,
        )

    alternative = select_provider_route(
        provider_decision_id=route_decision.provider_decision_id,
        turn_id=route_decision.turn_id,
        requirements=route_decision.requirements,
        candidates=affordable_routes,
        decided_at=decided_at,
    )
    return ProviderFailoverDecision(
        code=ProviderFailoverDecisionCode.FAILOVER,
        reason=(
            "Selected an unattempted route from the original policy-eligible "
            "set within the remaining cost."
        ),
        selected_route_id=alternative.selected_route_id,
        eligible_route_ids=tuple(
            route.route_id for route in alternative.eligible_routes
        ),
        cost_rejected_route_ids=cost_rejected,
        decided_at=decided_at,
    )


def _validated_attempts(
    attempted_route_ids: Sequence[RouteId],
) -> frozenset[RouteId]:
    if not 1 <= len(attempted_route_ids) <= MAXIMUM_PROVIDER_DECISION_ROUTES:
        raise ValueError("attempted provider route count is invalid")
    attempted = frozenset(attempted_route_ids)
    if len(attempted) != len(attempted_route_ids):
        raise ValueError("attempted provider routes must be unique")
    return attempted


def _validate_runtime(
    route_decision: ProviderRouteDecisionRecord,
    failure: ProviderTransportFailure,
    *,
    effect_state: ProviderAttemptEffectState,
    remaining_cost_microusd: int,
    decided_at: datetime,
) -> None:
    if not isinstance(effect_state, ProviderAttemptEffectState):
        raise ValueError("provider attempt effect state is invalid")
    if decided_at.tzinfo is None or decided_at.utcoffset() != timedelta(0):
        raise ValueError("provider failover decision time must use UTC")
    if failure.occurred_at < route_decision.decided_at:
        raise ValueError("provider failure precedes its route decision")
    if decided_at < failure.occurred_at:
        raise ValueError("provider failover decision precedes its failure")
    if not 0 <= remaining_cost_microusd <= MAXIMUM_FAILOVER_COST_MICROUSD:
        raise ValueError("remaining provider failover cost is invalid")


def _denied(
    code: ProviderFailoverDecisionCode,
    reason: str,
    decided_at: datetime,
    *,
    cost_rejected_route_ids: tuple[RouteId, ...] = (),
) -> ProviderFailoverDecision:
    return ProviderFailoverDecision(
        code=code,
        reason=reason,
        selected_route_id=None,
        eligible_route_ids=(),
        cost_rejected_route_ids=cost_rejected_route_ids,
        decided_at=decided_at,
    )
