from datetime import timedelta
from typing import cast

import pytest

from app.services.harness.protocol import (
    ProviderFailureClass,
    ProviderRetryDisposition,
    ProviderRouteRejectionCode,
    ProviderTransportFailure,
)
from app.services.harness.runtime import (
    ProviderAttemptEffectState,
    ProviderFailoverDecision,
    ProviderFailoverDecisionCode,
    plan_provider_failover,
)
from tests.harness.runtime.failover.fixtures import (
    NOW,
    REQUEST_SHA256,
    failure,
    route_decision,
)


def plan(
    *,
    transport_failure: ProviderTransportFailure | None = None,
    attempted_route_ids: tuple[str, ...] = ("openai.primary",),
    failed_route_id: str = "openai.primary",
    effect_state: ProviderAttemptEffectState = (
        ProviderAttemptEffectState.PRE_SIDE_EFFECT
    ),
    remaining_cost_microusd: int = 100,
) -> ProviderFailoverDecision:
    return plan_provider_failover(
        route_decision(),
        transport_failure or failure(),
        provider_request_sha256=REQUEST_SHA256,
        failed_route_id=failed_route_id,
        attempted_route_ids=attempted_route_ids,
        effect_state=effect_state,
        remaining_cost_microusd=remaining_cost_microusd,
        decided_at=NOW + timedelta(milliseconds=1),
    )


def test_transient_pre_side_effect_selects_policy_eligible_route() -> None:
    decision = plan()

    assert decision.failover
    assert decision.code is ProviderFailoverDecisionCode.FAILOVER
    assert decision.selected_route_id == "bedrock.secondary"
    assert decision.eligible_route_ids == (
        "bedrock.secondary",
        "vertex.tertiary",
    )
    assert decision.cost_rejected_route_ids == ()


def test_original_region_data_capability_and_cost_policy_is_preserved() -> None:
    original = route_decision()
    rejected = {
        route.route_id: route.rejection_codes
        for route in original.rejected_routes
    }
    next_decision = plan()

    assert rejected == {
        "local.policy": (
            ProviderRouteRejectionCode.DATA_CLASSIFICATION,
        ),
        "mock.incompatible": (
            ProviderRouteRejectionCode.CAPABILITY,
            ProviderRouteRejectionCode.COST,
        ),
        "openrouter.region": (ProviderRouteRejectionCode.REGION,),
    }
    assert not set(rejected).intersection(next_decision.eligible_route_ids)


def test_attempts_and_remaining_cost_constrain_next_route() -> None:
    decision = plan(remaining_cost_microusd=35)

    assert decision.selected_route_id == "vertex.tertiary"
    assert decision.eligible_route_ids == ("vertex.tertiary",)
    assert decision.cost_rejected_route_ids == ("bedrock.secondary",)


@pytest.mark.parametrize(
    ("transport_failure", "effect_state", "expected_code"),
    [
        (
            failure(
                failure_class=ProviderFailureClass.AUTHENTICATION,
                disposition=ProviderRetryDisposition.PROHIBITED,
            ),
            ProviderAttemptEffectState.PRE_SIDE_EFFECT,
            ProviderFailoverDecisionCode.FAILURE_INELIGIBLE,
        ),
        (
            failure(
                disposition=ProviderRetryDisposition.PROHIBITED,
                ambiguous=True,
            ),
            ProviderAttemptEffectState.PRE_SIDE_EFFECT,
            ProviderFailoverDecisionCode.AMBIGUOUS,
        ),
        (
            failure(),
            ProviderAttemptEffectState.SIDE_EFFECT_STARTED,
            ProviderFailoverDecisionCode.SIDE_EFFECT_STARTED,
        ),
        (
            failure(failure_class=ProviderFailureClass.RATE_LIMIT),
            ProviderAttemptEffectState.PRE_SIDE_EFFECT,
            ProviderFailoverDecisionCode.FAILURE_INELIGIBLE,
        ),
    ],
)
def test_only_transient_pre_side_effect_failure_can_fail_over(
    transport_failure: ProviderTransportFailure,
    effect_state: ProviderAttemptEffectState,
    expected_code: ProviderFailoverDecisionCode,
) -> None:
    decision = plan(
        transport_failure=transport_failure,
        effect_state=effect_state,
    )

    assert not decision.failover
    assert decision.code is expected_code
    assert decision.selected_route_id is None


def test_request_and_route_evidence_must_match() -> None:
    request_mismatch = plan(
        transport_failure=failure(request_sha256="8" * 64)
    )
    route_mismatch = plan(failed_route_id="bedrock.secondary")

    assert request_mismatch.code is (
        ProviderFailoverDecisionCode.REQUEST_MISMATCH
    )
    assert route_mismatch.code is ProviderFailoverDecisionCode.ROUTE_MISMATCH


def test_exhausted_routes_cannot_restart_failover_budget() -> None:
    decision = plan(
        attempted_route_ids=(
            "openai.primary",
            "bedrock.secondary",
            "vertex.tertiary",
        ),
        failed_route_id="vertex.tertiary",
    )

    assert not decision.failover
    assert decision.code is ProviderFailoverDecisionCode.NO_ALTERNATIVE


def test_malformed_attempt_evidence_fails_closed() -> None:
    with pytest.raises(ValueError, match="unique"):
        plan(
            attempted_route_ids=(
                "openai.primary",
                "openai.primary",
            )
        )
    with pytest.raises(ValueError, match="effect state"):
        plan(
            effect_state=cast(
                ProviderAttemptEffectState,
                "side_effect_started",
            )
        )
