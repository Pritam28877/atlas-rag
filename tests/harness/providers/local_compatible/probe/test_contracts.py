from pathlib import Path

import pytest

from app.cli.harness.local_probe_contracts import (
    LocalProbeGateError,
    LocalProbeGateErrorCode,
    LocalProbeLaunchRequest,
    authorize_local_probe,
)


def test_admission_binds_exactly_six_calls_and_fresh_evidence(
    tmp_path: Path,
) -> None:
    authorized = authorize_local_probe(_request(tmp_path))

    assert authorized.maximum_provider_calls == 6
    assert authorized.evidence_ttl_seconds == 900


@pytest.mark.parametrize(
    ("update", "code"),
    (
        ({"acknowledged": False}, LocalProbeGateErrorCode.ACKNOWLEDGEMENT),
        (
            {"gate_environment_variable": "unsafe-name"},
            LocalProbeGateErrorCode.ENVIRONMENT,
        ),
        (
            {"evidence_ttl_seconds": 60},
            LocalProbeGateErrorCode.TTL,
        ),
        (
            {"result_path": Path("relative.json")},
            LocalProbeGateErrorCode.PATH,
        ),
    ),
)
def test_admission_rejects_unbounded_or_ambiguous_launches(
    tmp_path: Path,
    update: dict[str, object],
    code: LocalProbeGateErrorCode,
) -> None:
    request = _request(tmp_path).model_copy(update=update)

    with pytest.raises(LocalProbeGateError) as rejected:
        authorize_local_probe(request)

    assert rejected.value.code is code


def _request(tmp_path: Path) -> LocalProbeLaunchRequest:
    return LocalProbeLaunchRequest(
        acknowledged=True,
        model="configured-local-model",
        per_case_timeout_seconds=10,
        evidence_ttl_seconds=900,
        gate_environment_variable="ATLAS_LOCAL_PROBE_ENABLED",
        credential_environment_variable=None,
        configuration_path=tmp_path / "providers.json",
        route_policy_path=tmp_path / "route.json",
        identity_path=tmp_path / "identity.json",
        result_path=tmp_path / "capabilities.json",
    )
