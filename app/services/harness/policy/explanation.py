"""Secret-free client explanation for effective policy decisions."""

from pydantic import Field

from app.services.harness.policy.models import (
    CapabilityKind,
    CapabilityLimits,
    EffectivePolicyDecision,
    PolicyEffect,
    PolicyLayer,
    PolicyRuleId,
)
from app.services.harness.protocol import (
    Capability,
    DecisionId,
    PolicyVersion,
    StrictProtocolModel,
    UtcTimestamp,
)


class ExplainedPolicyRule(StrictProtocolModel):
    layer: PolicyLayer
    policy_version: PolicyVersion
    rule_id: PolicyRuleId
    effect: PolicyEffect
    target_matched: bool


class ExplainedPolicyLayer(StrictProtocolModel):
    layer: PolicyLayer
    policy_version: PolicyVersion
    effect: PolicyEffect
    matched_rule_ids: tuple[PolicyRuleId, ...] = Field(max_length=64)
    used_default: bool


class PolicyDecisionExplanation(StrictProtocolModel):
    decision_id: DecisionId
    effect: PolicyEffect
    capability: Capability
    capability_kind: CapabilityKind
    policy_bundle_version: PolicyVersion
    layers: tuple[ExplainedPolicyLayer, ...] = Field(min_length=1, max_length=5)
    evaluated_rules: tuple[ExplainedPolicyRule, ...] = Field(max_length=320)
    effective_limits: CapabilityLimits
    evaluated_at: UtcTimestamp
    redacted_fields: tuple[str, ...] = (
        "arguments",
        "secret_handles",
        "structured_targets",
    )


def explain_policy_decision(
    decision: EffectivePolicyDecision,
) -> PolicyDecisionExplanation:
    return PolicyDecisionExplanation(
        decision_id=decision.decision_id,
        effect=decision.effect,
        capability=decision.capability,
        capability_kind=decision.capability_kind,
        policy_bundle_version=decision.policy_bundle_version,
        layers=tuple(
            ExplainedPolicyLayer(
                layer=layer.layer,
                policy_version=layer.policy_version,
                effect=layer.effect,
                matched_rule_ids=layer.matched_rule_ids,
                used_default=layer.used_default,
            )
            for layer in decision.layer_decisions
        ),
        evaluated_rules=tuple(
            ExplainedPolicyRule(
                layer=rule.layer,
                policy_version=rule.policy_version,
                rule_id=rule.rule_id,
                effect=rule.effect,
                target_matched=rule.target_matched,
            )
            for rule in decision.evaluated_rules
        ),
        effective_limits=decision.effective_limits,
        evaluated_at=decision.evaluated_at,
    )
