from datetime import timedelta

import pytest

from app.services.harness.providers.local_compatible_capabilities import (
    LocalCompatibleFeature,
    decide_local_compatible_request,
)
from app.services.harness.providers.local_compatible_compiler import (
    LocalCompatibleCompileError,
    LocalCompatibleCompileErrorCode,
    LocalCompatibleResponsesCompiler,
)
from tests.harness.providers.local_compatible.fixtures import (
    BASE_FEATURES,
    NOW,
    accepted_decision,
    authorized_route,
    context,
    local_model,
    local_request,
    probe,
)


def test_accepted_probe_compiles_exact_responses_contract() -> None:
    compiled = LocalCompatibleResponsesCompiler().compile(
        local_request(),
        local_model(),
        context(),
        accepted_decision(),
    )

    assert compiled.model == "configured-local-model"
    assert compiled.stream is True
    assert compiled.store is False
    assert compiled.truncation == "disabled"


def test_missing_capability_is_rejected_before_compilation() -> None:
    limited = tuple(
        feature
        for feature in BASE_FEATURES
        if feature is not LocalCompatibleFeature.STREAM_USAGE
    )
    decision = decide_local_compatible_request(
        local_request(),
        context(),
        authorized_route(),
        probe(limited),
        decided_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(LocalCompatibleCompileError) as captured:
        LocalCompatibleResponsesCompiler().compile(
            local_request(),
            local_model(),
            context(),
            decision,
        )

    assert captured.value.code is LocalCompatibleCompileErrorCode.CAPABILITY


def test_substituted_or_too_short_evidence_is_rejected() -> None:
    substituted = accepted_decision().model_copy(
        update={"provider_request_sha256": "9" * 64}
    )
    expires_before_request = accepted_decision().model_copy(
        update={"valid_until": local_request().deadline_at - timedelta(seconds=1)}
    )

    for decision in (substituted, expires_before_request):
        with pytest.raises(LocalCompatibleCompileError) as captured:
            LocalCompatibleResponsesCompiler().compile(
                local_request(),
                local_model(),
                context(),
                decision,
            )
        assert captured.value.code is LocalCompatibleCompileErrorCode.EVIDENCE
