"""Deny-first layered policy compiler properties."""

from itertools import product

from app.services.harness.policy import (
    PolicyEffect,
    PolicyLayer,
    compile_capability_decision,
)
from tests.harness.policy.fixtures import (
    DECISION_ID,
    NOW,
    TRACE,
    bundle,
    document,
    proposal,
    rule,
)

EXPECTED_INTERSECTION = {
    (PolicyEffect.ALLOW, PolicyEffect.ALLOW): PolicyEffect.ALLOW,
    (PolicyEffect.ALLOW, PolicyEffect.ASK): PolicyEffect.ASK,
    (PolicyEffect.ALLOW, PolicyEffect.DENY): PolicyEffect.DENY,
    (PolicyEffect.ASK, PolicyEffect.ALLOW): PolicyEffect.ASK,
    (PolicyEffect.ASK, PolicyEffect.ASK): PolicyEffect.ASK,
    (PolicyEffect.ASK, PolicyEffect.DENY): PolicyEffect.DENY,
    (PolicyEffect.DENY, PolicyEffect.ALLOW): PolicyEffect.DENY,
    (PolicyEffect.DENY, PolicyEffect.ASK): PolicyEffect.DENY,
    (PolicyEffect.DENY, PolicyEffect.DENY): PolicyEffect.DENY,
}


def compile_effects(
    system_effect: PolicyEffect,
    workspace_effect: PolicyEffect,
):
    return compile_capability_decision(
        decision_id=DECISION_ID,
        proposal=proposal(),
        policies=bundle(
            document(
                PolicyLayer.SYSTEM,
                rule("system-rule", system_effect),
            ),
            document(
                PolicyLayer.WORKSPACE,
                rule("workspace-rule", workspace_effect),
            ),
        ),
        evaluated_at=NOW,
        trace=TRACE,
    )


def test_all_effect_pairs_follow_deny_first_intersection() -> None:
    for system_effect, workspace_effect in product(PolicyEffect, repeat=2):
        decision = compile_effects(system_effect, workspace_effect)

        assert decision.effect is EXPECTED_INTERSECTION[
            (system_effect, workspace_effect)
        ]
        assert len(decision.layer_decisions) == 2


def test_adding_a_restrictive_layer_never_broadens_access() -> None:
    access_rank = {
        PolicyEffect.DENY: 0,
        PolicyEffect.ASK: 1,
        PolicyEffect.ALLOW: 2,
    }
    baseline = compile_capability_decision(
        decision_id=DECISION_ID,
        proposal=proposal(),
        policies=bundle(
            document(
                PolicyLayer.SYSTEM,
                rule("system-rule", PolicyEffect.ALLOW),
            )
        ),
        evaluated_at=NOW,
        trace=TRACE,
    )

    for restrictive_effect in (PolicyEffect.ASK, PolicyEffect.DENY):
        restricted = compile_effects(PolicyEffect.ALLOW, restrictive_effect)
        assert access_rank[restricted.effect] <= access_rank[baseline.effect]


def test_same_layer_deny_wins_regardless_of_rule_order() -> None:
    decision = compile_capability_decision(
        decision_id=DECISION_ID,
        proposal=proposal(),
        policies=bundle(
            document(
                PolicyLayer.SYSTEM,
                rule("allow-rule", PolicyEffect.ALLOW),
                rule("deny-rule", PolicyEffect.DENY),
            )
        ),
        evaluated_at=NOW,
        trace=TRACE,
    )

    assert decision.effect is PolicyEffect.DENY
    assert decision.layer_decisions[0].matched_rule_ids == (
        "allow-rule",
        "deny-rule",
    )


def test_unmatched_target_fails_closed_with_evaluation_evidence() -> None:
    different_target = "f" * 64
    unmatched_rule = rule("target-rule", PolicyEffect.ALLOW).model_copy(
        update={"target_sha256": different_target}
    )

    decision = compile_capability_decision(
        decision_id=DECISION_ID,
        proposal=proposal(),
        policies=bundle(document(PolicyLayer.SYSTEM, unmatched_rule)),
        evaluated_at=NOW,
        trace=TRACE,
    )

    assert decision.effect is PolicyEffect.DENY
    assert decision.layer_decisions[0].used_default
    assert not decision.evaluated_rules[0].target_matched


def test_policy_limits_only_narrow_sandbox_limits() -> None:
    decision = compile_capability_decision(
        decision_id=DECISION_ID,
        proposal=proposal(),
        policies=bundle(
            document(
                PolicyLayer.SYSTEM,
                rule(
                    "bounded-rule",
                    PolicyEffect.ALLOW,
                    duration_ms=2_000,
                ),
            )
        ),
        evaluated_at=NOW,
        trace=TRACE,
    )

    assert decision.effective_limits.max_duration_ms == 2_000
    assert decision.effective_limits.max_memory_bytes == 512 * 1024 * 1024


def test_all_matching_rules_retain_the_strictest_limit() -> None:
    decision = compile_capability_decision(
        decision_id=DECISION_ID,
        proposal=proposal(),
        policies=bundle(
            document(
                PolicyLayer.SYSTEM,
                rule(
                    "allow-rule",
                    PolicyEffect.ALLOW,
                    duration_ms=1_000,
                ),
                rule(
                    "ask-rule",
                    PolicyEffect.ASK,
                    duration_ms=8_000,
                ),
            )
        ),
        evaluated_at=NOW,
        trace=TRACE,
    )

    assert decision.effect is PolicyEffect.ASK
    assert decision.effective_limits.max_duration_ms == 1_000
