"""Policy lint finding coverage."""

from app.services.harness.policy import (
    CapabilityKind,
    PolicyEffect,
    PolicyLayer,
    PolicyLintCode,
    PolicyRule,
    lint_policy_bundle,
)
from tests.harness.policy.fixtures import bundle, document, proposal, rule


def test_lint_detects_conflict_shadow_and_overbroad_rule() -> None:
    report = lint_policy_bundle(
        bundle(
            document(
                PolicyLayer.SYSTEM,
                rule("allow-rule", PolicyEffect.ALLOW),
                rule("deny-rule", PolicyEffect.DENY),
                rule(
                    "wide-rule",
                    PolicyEffect.ASK,
                    exact_target=False,
                ),
            )
        )
    )

    codes = {finding.code for finding in report.findings}
    assert codes == {
        PolicyLintCode.CONFLICTING_RULES,
        PolicyLintCode.OVERBROAD_TARGET,
        PolicyLintCode.SHADOWED_RULE,
    }


def test_lint_detects_kind_namespace_mismatch_without_target_data() -> None:
    invalid_namespace = PolicyRule(
        rule_id="namespace-rule",
        capability_kind=CapabilityKind.NETWORK,
        capability="filesystem.write",
        effect=PolicyEffect.DENY,
        target_sha256=proposal().target_sha256,
    )
    report = lint_policy_bundle(
        bundle(document(PolicyLayer.SYSTEM, invalid_namespace))
    )

    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.code is PolicyLintCode.INVALID_CAPABILITY_NAMESPACE
    serialized = finding.model_dump_json()
    assert proposal().target_sha256 not in serialized
    assert "src/app.py" not in serialized


def test_lint_report_is_deterministic() -> None:
    policies = bundle(
        document(
            PolicyLayer.SYSTEM,
            rule("allow-rule", PolicyEffect.ALLOW),
            rule("deny-rule", PolicyEffect.DENY),
        )
    )

    assert lint_policy_bundle(policies) == lint_policy_bundle(policies)
