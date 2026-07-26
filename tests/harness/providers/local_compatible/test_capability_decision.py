import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from app.services.harness.protocol import (
    ProviderContextPlan,
    ProviderMessage,
    ProviderMessageRole,
    ProviderToolDefinition,
)
from app.services.harness.providers.local_compatible_capabilities import (
    LocalCompatibleFeature,
    LocalCompatibleProbe,
    LocalCompatibleProbeError,
    LocalCompatibleProbeErrorCode,
    decide_local_compatible_request,
)
from app.services.harness.providers.local_compatible_policy import (
    authorize_local_compatible_route,
)
from tests.harness.providers.local_compatible.fixtures import (
    DESTINATION_SHA256,
    MODEL_ID,
    configuration,
    identity,
    route_policy,
)
from tests.harness_provider_capability_fixtures import model, request

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
BASE_FEATURES = (
    LocalCompatibleFeature.MODALITY_TEXT,
    LocalCompatibleFeature.RESPONSES_STREAM,
    LocalCompatibleFeature.STORE_FALSE,
    LocalCompatibleFeature.STREAM_CANCEL,
    LocalCompatibleFeature.STREAM_USAGE,
    LocalCompatibleFeature.TRUNCATION_DISABLED,
)


def local_request():
    return request().model_copy(update={"route_id": "local.primary"})


def context(canonical_request=None) -> ProviderContextPlan:
    selected = canonical_request or local_request()
    return ProviderContextPlan(
        provider_request_sha256=hashlib.sha256(
            selected.model_dump_json().encode()
        ).hexdigest(),
        model_revision_sha256=model().model_revision_sha256,
        applied_features=(),
        estimated_input_tokens=10,
        reason="Local capability decision context.",
    )


def probe(
    features: tuple[LocalCompatibleFeature, ...] = BASE_FEATURES,
    **updates: object,
) -> LocalCompatibleProbe:
    values = {
        "route_id": "local.primary",
        "model_id": MODEL_ID,
        "model_revision_sha256": model().model_revision_sha256,
        "destination_sha256": DESTINATION_SHA256,
        "supported_features": features,
        "evidence_sha256": "a" * 64,
        "observed_at": NOW,
        "expires_at": NOW + timedelta(minutes=15),
    }
    values.update(updates)
    return LocalCompatibleProbe.model_validate(values)


def authorized_route():
    loaded, route = configuration()
    return authorize_local_compatible_route(
        loaded,
        route,
        route_policy(),
        identity(),
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
