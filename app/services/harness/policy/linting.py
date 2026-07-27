"""Deterministic semantic linting for canonical policy bundles."""

from collections import defaultdict
from enum import StrEnum

from pydantic import Field

from app.services.harness.policy.models import (
    POLICY_LAYER_ORDER,
    CapabilityKind,
    PolicyBundle,
    PolicyEffect,
    PolicyLayer,
    PolicyRule,
    PolicyRuleId,
    policy_bundle_version,
)
from app.services.harness.protocol import PolicyVersion, StrictProtocolModel


class PolicyLintCode(StrEnum):
    CONFLICTING_RULES = "conflicting_rules"
    INVALID_CAPABILITY_NAMESPACE = "invalid_capability_namespace"
    OVERBROAD_TARGET = "overbroad_target"
    SHADOWED_RULE = "shadowed_rule"


class PolicyLintSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class PolicyLintFinding(StrictProtocolModel):
    code: PolicyLintCode
    severity: PolicyLintSeverity
    layer: PolicyLayer
    rule_ids: tuple[PolicyRuleId, ...] = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=256)


class PolicyLintReport(StrictProtocolModel):
    policy_bundle_version: PolicyVersion
    findings: tuple[PolicyLintFinding, ...] = Field(max_length=320)


def lint_policy_bundle(bundle: PolicyBundle) -> PolicyLintReport:
    findings: list[PolicyLintFinding] = []
    for document in bundle.documents:
        findings.extend(_lint_rule_semantics(document.layer, document.rules))
        findings.extend(_lint_selector_groups(document.layer, document.rules))
    findings.sort(
        key=lambda finding: (
            POLICY_LAYER_ORDER[finding.layer],
            finding.code.value,
            finding.rule_ids,
        )
    )
    return PolicyLintReport(
        policy_bundle_version=policy_bundle_version(bundle),
        findings=tuple(findings),
    )


def _lint_rule_semantics(
    layer: PolicyLayer,
    rules: tuple[PolicyRule, ...],
) -> list[PolicyLintFinding]:
    findings: list[PolicyLintFinding] = []
    for rule in rules:
        expected_namespace = f"{rule.capability_kind.value}."
        if not rule.capability.startswith(expected_namespace):
            findings.append(
                PolicyLintFinding(
                    code=PolicyLintCode.INVALID_CAPABILITY_NAMESPACE,
                    severity=PolicyLintSeverity.ERROR,
                    layer=layer,
                    rule_ids=(rule.rule_id,),
                    message="Capability name does not match its structured kind.",
                )
            )
        if rule.target_sha256 is None and rule.effect is not PolicyEffect.DENY:
            findings.append(
                PolicyLintFinding(
                    code=PolicyLintCode.OVERBROAD_TARGET,
                    severity=PolicyLintSeverity.WARNING,
                    layer=layer,
                    rule_ids=(rule.rule_id,),
                    message="Allow or ask rule applies to every structured target.",
                )
            )
    return findings


def _lint_selector_groups(
    layer: PolicyLayer,
    rules: tuple[PolicyRule, ...],
) -> list[PolicyLintFinding]:
    groups: dict[
        tuple[CapabilityKind, str, str | None],
        list[PolicyRule],
    ] = defaultdict(list)
    for rule in rules:
        selector = (
            rule.capability_kind,
            rule.capability,
            rule.target_sha256,
        )
        groups[selector].append(rule)

    findings: list[PolicyLintFinding] = []
    for selector_rules in groups.values():
        effects = {rule.effect for rule in selector_rules}
        if len(effects) > 1:
            findings.append(
                PolicyLintFinding(
                    code=PolicyLintCode.CONFLICTING_RULES,
                    severity=PolicyLintSeverity.WARNING,
                    layer=layer,
                    rule_ids=tuple(rule.rule_id for rule in selector_rules),
                    message="Same selector has multiple effects; deny-first applies.",
                )
            )
        if PolicyEffect.DENY in effects:
            shadowed_ids = tuple(
                rule.rule_id
                for rule in selector_rules
                if rule.effect is not PolicyEffect.DENY
            )
            if shadowed_ids:
                findings.append(
                    PolicyLintFinding(
                        code=PolicyLintCode.SHADOWED_RULE,
                        severity=PolicyLintSeverity.WARNING,
                        layer=layer,
                        rule_ids=shadowed_ids,
                        message="Deny rule shadows weaker rules for this selector.",
                    )
                )
        findings.extend(_duplicate_findings(layer, selector_rules))
    return findings


def _duplicate_findings(
    layer: PolicyLayer,
    rules: list[PolicyRule],
) -> list[PolicyLintFinding]:
    first_by_behavior: dict[tuple[PolicyEffect, str], PolicyRuleId] = {}
    findings: list[PolicyLintFinding] = []
    for rule in rules:
        behavior = (rule.effect, rule.limits.model_dump_json())
        first_rule_id = first_by_behavior.setdefault(behavior, rule.rule_id)
        if first_rule_id != rule.rule_id:
            findings.append(
                PolicyLintFinding(
                    code=PolicyLintCode.SHADOWED_RULE,
                    severity=PolicyLintSeverity.WARNING,
                    layer=layer,
                    rule_ids=(rule.rule_id,),
                    message="Rule duplicates an earlier selector and behavior.",
                )
            )
    return findings
