from pathlib import Path

import pytest

from app.cli.harness.adapter_smoke_contracts import (
    AdapterSmokeGateError,
    AdapterSmokeGateErrorCode,
    AdapterSmokeLaunchRequest,
    authorize_adapter_smoke,
)


def test_vertex_requires_disposable_project_and_positive_cap(
    tmp_path: Path,
) -> None:
    authorized = authorize_adapter_smoke(
        _request(
            tmp_path,
            provider="vertex",
            disposable_project_id="atlas-smoke-12345",
            cost_cap_microusd=10_000,
        )
    )

    assert authorized.maximum_provider_calls == 1
    assert authorized.disposable_project_id == "atlas-smoke-12345"
    assert authorized.credential_environment_variable is None


def test_local_requires_zero_cloud_cap_and_allows_optional_bearer(
    tmp_path: Path,
) -> None:
    authorized = authorize_adapter_smoke(
        _request(
            tmp_path,
            provider="local-compatible",
            cost_cap_microusd=0,
            credential_environment_variable="ATLAS_LOCAL_SMOKE_TOKEN",
        )
    )

    assert authorized.cost_cap_microusd == 0
    assert authorized.disposable_project_id is None
    assert (
        authorized.credential_environment_variable
        == "ATLAS_LOCAL_SMOKE_TOKEN"
    )


@pytest.mark.parametrize(
    ("updates", "expected"),
    (
        (
            {"acknowledged": False},
            AdapterSmokeGateErrorCode.ACKNOWLEDGEMENT,
        ),
        (
            {"max_output_tokens": 257},
            AdapterSmokeGateErrorCode.TOKEN_CAP,
        ),
        (
            {"timeout_seconds": 31},
            AdapterSmokeGateErrorCode.TIMEOUT,
        ),
        (
            {"cost_cap_microusd": None},
            AdapterSmokeGateErrorCode.BUDGET,
        ),
        (
            {"gate_environment_variable": "unsafe-name"},
            AdapterSmokeGateErrorCode.ENVIRONMENT,
        ),
        (
            {"configuration_path": Path("relative.json")},
            AdapterSmokeGateErrorCode.PATH,
        ),
    ),
)
def test_admission_fails_in_deterministic_order(
    tmp_path: Path,
    updates: dict[str, object],
    expected: AdapterSmokeGateErrorCode,
) -> None:
    with pytest.raises(AdapterSmokeGateError) as captured:
        authorize_adapter_smoke(_request(tmp_path, **updates))

    assert captured.value.code is expected
    assert str(captured.value) == "adapter live smoke admission failed"


@pytest.mark.parametrize(
    "updates",
    (
        {
            "provider": "vertex",
            "cost_cap_microusd": 1,
            "disposable_project_id": None,
        },
        {
            "provider": "vertex",
            "cost_cap_microusd": 0,
            "disposable_project_id": "atlas-smoke-12345",
        },
        {
            "provider": "vertex",
            "cost_cap_microusd": 1,
            "disposable_project_id": "atlas-smoke-12345",
            "credential_environment_variable": "RAW_VERTEX_TOKEN",
        },
        {
            "provider": "local-compatible",
            "cost_cap_microusd": 1,
        },
        {
            "provider": "local-compatible",
            "cost_cap_microusd": 0,
            "disposable_project_id": "atlas-smoke-12345",
        },
    ),
)
def test_cross_provider_budget_or_identity_substitution_is_rejected(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    with pytest.raises(AdapterSmokeGateError) as captured:
        authorize_adapter_smoke(_request(tmp_path, **updates))

    assert captured.value.code is AdapterSmokeGateErrorCode.PROVIDER


def _request(
    tmp_path: Path,
    **updates: object,
) -> AdapterSmokeLaunchRequest:
    values = {
        "acknowledged": True,
        "provider": "local-compatible",
        "model": "configured-smoke-model",
        "max_output_tokens": 32,
        "timeout_seconds": 10,
        "cost_cap_microusd": 0,
        "disposable_project_id": None,
        "gate_environment_variable": "ATLAS_ADAPTER_SMOKE_ENABLED",
        "credential_environment_variable": None,
        "configuration_path": tmp_path / "providers.json",
        "route_policy_path": tmp_path / "route.json",
        "identity_path": tmp_path / "identity.json",
        "database_path": tmp_path / "smoke.sqlite3",
        "result_path": tmp_path / "result.json",
    }
    values.update(updates)
    return AdapterSmokeLaunchRequest.model_validate(values)
