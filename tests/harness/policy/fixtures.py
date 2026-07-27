"""Canonical capability policy test fixtures."""

from datetime import UTC, datetime

from app.services.harness.policy import (
    CapabilityKind,
    CapabilityLimits,
    CapabilityProposal,
    FilesystemOperation,
    FilesystemTarget,
    PolicyBundle,
    PolicyDocument,
    PolicyEffect,
    PolicyLayer,
    PolicyRule,
    capability_target_sha256,
    policy_document_version,
)
from app.services.harness.protocol import TraceLink

NOW = datetime(2026, 7, 27, tzinfo=UTC)
DECISION_ID = "dcs_" + "1" * 32
TRACE = TraceLink(
    request_id="req_" + "2" * 32,
    correlation_id="evt_" + "3" * 32,
)


def proposal() -> CapabilityProposal:
    target = FilesystemTarget(
        root_uri="file:///workspace",
        relative_path="src/app.py",
        operation=FilesystemOperation.WRITE,
    )
    return CapabilityProposal(
        capability="filesystem.write",
        target=target,
        target_sha256=capability_target_sha256(target),
        sandbox_limits=CapabilityLimits(
            max_duration_ms=10_000,
            max_cpu_ms=5_000,
            max_memory_bytes=512 * 1024 * 1024,
            max_output_bytes=1_000_000,
            max_processes=8,
        ),
    )


def rule(
    rule_id: str,
    effect: PolicyEffect,
    *,
    exact_target: bool = True,
    duration_ms: int | None = None,
) -> PolicyRule:
    return PolicyRule(
        rule_id=rule_id,
        capability_kind=CapabilityKind.FILESYSTEM,
        capability="filesystem.write",
        effect=effect,
        target_sha256=proposal().target_sha256 if exact_target else None,
        limits=CapabilityLimits(max_duration_ms=duration_ms),
    )


def document(
    layer: PolicyLayer,
    *rules: PolicyRule,
) -> PolicyDocument:
    ordered_rules = tuple(sorted(rules, key=lambda item: item.rule_id))
    return PolicyDocument(
        layer=layer,
        policy_version=policy_document_version(
            layer=layer,
            rules=ordered_rules,
        ),
        rules=ordered_rules,
    )


def bundle(*documents: PolicyDocument) -> PolicyBundle:
    return PolicyBundle(documents=documents)
