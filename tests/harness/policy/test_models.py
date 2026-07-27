"""Structured capability target validation."""

import pytest
from pydantic import ValidationError

from app.services.harness.policy import (
    CapabilityKind,
    ExecutableTarget,
    NetworkMethod,
    NetworkTarget,
    PolicyBundle,
    PolicyEffect,
    PolicyLayer,
    SecretTarget,
)
from tests.harness.policy.fixtures import document, rule


@pytest.mark.parametrize("effect", tuple(PolicyEffect))
def test_policy_version_is_bound_to_rule_effect(effect: PolicyEffect) -> None:
    first = document(PolicyLayer.SYSTEM, rule("effect-rule", effect))
    second_effect = (
        PolicyEffect.DENY if effect is not PolicyEffect.DENY else PolicyEffect.ALLOW
    )
    second = document(PolicyLayer.SYSTEM, rule("effect-rule", second_effect))

    assert first.policy_version != second.policy_version


def test_shell_string_cannot_replace_structured_executable() -> None:
    with pytest.raises(ValidationError, match="canonical absolute"):
        ExecutableTarget(
            executable="python -c 'unsafe()'",
            arguments=(),
            working_directory=".",
        )


def test_network_host_rejects_embedded_scheme() -> None:
    with pytest.raises(ValidationError, match="canonical lowercase host"):
        NetworkTarget(
            scheme="https",
            host="https://example.com",
            port=443,
            method=NetworkMethod.POST,
        )


def test_secret_target_contains_handle_and_destination_hash_only() -> None:
    target = SecretTarget(
        secret_handle="secret:provider/openai",
        destination_sha256="a" * 64,
    )

    assert target.model_dump() == {
        "kind": CapabilityKind.SECRET,
        "secret_handle": "secret:provider/openai",
        "destination_sha256": "a" * 64,
    }


def test_bundle_requires_unique_canonical_layers() -> None:
    system = document(
        PolicyLayer.SYSTEM,
        rule("system-rule", PolicyEffect.ALLOW),
    )
    with pytest.raises(ValidationError, match="unique canonical layers"):
        PolicyBundle(documents=(system, system))
