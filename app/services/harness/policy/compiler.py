"""Deterministic deny-first compilation for layered capability policies."""

from __future__ import annotations

from datetime import datetime

from app.services.harness.policy.models import (
    CapabilityLimits,
    CapabilityProposal,
    EffectivePolicyDecision,
    EvaluatedPolicyRule,
    LayerPolicyDecision,
    PolicyBundle,
    PolicyDocument,
    PolicyEffect,
    PolicyRule,
    policy_bundle_version,
)
from app.services.harness.protocol import DecisionId, TraceLink

_EFFECT_PRECEDENCE = {
    PolicyEffect.ALLOW: 0,
    PolicyEffect.ASK: 1,
    PolicyEffect.DENY: 2,
}
_LIMIT_FIELDS = (
    "max_duration_ms",
    "max_cpu_ms",
    "max_memory_bytes",
    "max_output_bytes",
    "max_processes",
)


def compile_capability_decision(
    *,
    decision_id: DecisionId,
    proposal: CapabilityProposal,
    policies: PolicyBundle,
    evaluated_at: datetime,
    trace: TraceLink,
) -> EffectivePolicyDecision:
    """Intersect active layers without allowing a weaker layer to broaden access."""
    layer_decisions: list[LayerPolicyDecision] = []
    evaluated_rules: list[EvaluatedPolicyRule] = []
    selected_rules: list[PolicyRule] = []

    for document in policies.documents:
        layer_decision, layer_evaluations, layer_selected = _evaluate_layer(
            document,
            proposal,
        )
        layer_decisions.append(layer_decision)
        evaluated_rules.extend(layer_evaluations)
        selected_rules.extend(layer_selected)

    effect = max(
        (decision.effect for decision in layer_decisions),
        key=_EFFECT_PRECEDENCE.__getitem__,
    )
    effective_limits = _intersect_limits(
        proposal.sandbox_limits,
        tuple(rule.limits for rule in selected_rules),
    )
    return EffectivePolicyDecision(
        decision_id=decision_id,
        effect=effect,
        capability=proposal.capability,
        capability_kind=proposal.target.kind,
        request_sha256=proposal.target_sha256,
        policy_bundle_version=policy_bundle_version(policies),
        layer_decisions=tuple(layer_decisions),
        evaluated_rules=tuple(evaluated_rules),
        effective_limits=effective_limits,
        evaluated_at=evaluated_at,
        trace=trace,
    )


def _evaluate_layer(
    document: PolicyDocument,
    proposal: CapabilityProposal,
) -> tuple[
    LayerPolicyDecision,
    tuple[EvaluatedPolicyRule, ...],
    tuple[PolicyRule, ...],
]:
    candidate_rules = tuple(
        rule
        for rule in document.rules
        if rule.capability_kind is proposal.target.kind
        and rule.capability == proposal.capability
    )
    evaluations: list[EvaluatedPolicyRule] = []
    matched: list[PolicyRule] = []
    for rule in candidate_rules:
        target_matched = (
            rule.target_sha256 is None
            or rule.target_sha256 == proposal.target_sha256
        )
        evaluations.append(
            EvaluatedPolicyRule(
                layer=document.layer,
                policy_version=document.policy_version,
                rule_id=rule.rule_id,
                effect=rule.effect,
                target_matched=target_matched,
            )
        )
        if target_matched:
            matched.append(rule)

    if not matched:
        return (
            LayerPolicyDecision(
                layer=document.layer,
                policy_version=document.policy_version,
                effect=PolicyEffect.DENY,
                matched_rule_ids=(),
                used_default=True,
            ),
            tuple(evaluations),
            (),
        )

    effect = max(
        (rule.effect for rule in matched),
        key=_EFFECT_PRECEDENCE.__getitem__,
    )
    return (
        LayerPolicyDecision(
            layer=document.layer,
            policy_version=document.policy_version,
            effect=effect,
            matched_rule_ids=tuple(rule.rule_id for rule in matched),
            used_default=False,
        ),
        tuple(evaluations),
        tuple(matched),
    )


def _intersect_limits(
    sandbox_limits: CapabilityLimits,
    policy_limits: tuple[CapabilityLimits, ...],
) -> CapabilityLimits:
    intersected: dict[str, int | None] = {}
    for field_name in _LIMIT_FIELDS:
        values = [
            value
            for limits in (sandbox_limits, *policy_limits)
            if (value := getattr(limits, field_name)) is not None
        ]
        intersected[field_name] = min(values) if values else None
    return CapabilityLimits.model_validate(intersected)
