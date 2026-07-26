import hashlib
from datetime import timedelta

import pytest

from app.services.harness.protocol import (
    ProviderMessage,
    ProviderMessageRole,
    ProviderToolDefinition,
)
from app.services.harness.providers.local_compatible_capabilities import (
    LocalCompatibleFeature,
    LocalCompatibleProbeError,
    LocalCompatibleProbeErrorCode,
    decide_local_compatible_request,
)
from tests.harness.providers.local_compatible.fixtures import (
    BASE_FEATURES,
    NOW,
    authorized_route,
    context,
    local_request,
    probe,
)


def test_base_stream_usage_and_cancel_capabilities_are_accepted() -> None:
    decision = decide_local_compatible_request(
        local_request(),
        context(),
        authorized_route(),
        probe(),
        decided_at=NOW + timedelta(minutes=1),
    )

    assert decision.accepted
    assert decision.missing_features == ()


def test_missing_usage_cancel_and_tools_are_explicit_rejections() -> None:
    schema_json = '{"properties":{},"type":"object"}'
    tool = ProviderToolDefinition(
        name="lookup",
        version="1.0.0",
        description="Look up a value.",
        input_schema_json=schema_json,
        input_schema_sha256=hashlib.sha256(schema_json.encode()).hexdigest(),
    )
    developer = ProviderMessage(
        role=ProviderMessageRole.DEVELOPER,
        parts=local_request().messages[0].parts,
    )
    canonical_request = local_request().model_copy(
        update={
            "messages": (developer, *local_request().messages),
            "tools": (tool,),
        }
    )
    limited_features = tuple(
        feature
        for feature in BASE_FEATURES
        if feature
        not in {
            LocalCompatibleFeature.STREAM_CANCEL,
            LocalCompatibleFeature.STREAM_USAGE,
        }
    )

    decision = decide_local_compatible_request(
        canonical_request,
        context(canonical_request),
        authorized_route(),
        probe(limited_features),
        decided_at=NOW + timedelta(minutes=1),
    )

    assert not decision.accepted
    assert decision.missing_features == (
        LocalCompatibleFeature.ROLE_DEVELOPER,
        LocalCompatibleFeature.STREAM_CANCEL,
        LocalCompatibleFeature.STREAM_USAGE,
        LocalCompatibleFeature.TOOLS_FUNCTION,
        LocalCompatibleFeature.TOOLS_PARALLEL,
        LocalCompatibleFeature.TOOLS_STRICT,
    )


def test_stale_and_substituted_probe_evidence_fail_closed() -> None:
    with pytest.raises(LocalCompatibleProbeError) as stale:
        decide_local_compatible_request(
            local_request(),
            context(),
            authorized_route(),
            probe(),
            decided_at=NOW + timedelta(minutes=16),
        )
    assert stale.value.code is LocalCompatibleProbeErrorCode.STALE

    with pytest.raises(LocalCompatibleProbeError) as substituted:
        decide_local_compatible_request(
            local_request(),
            context(),
            authorized_route(),
            probe(destination_sha256="9" * 64),
            decided_at=NOW + timedelta(minutes=1),
        )
    assert substituted.value.code is LocalCompatibleProbeErrorCode.BINDING
