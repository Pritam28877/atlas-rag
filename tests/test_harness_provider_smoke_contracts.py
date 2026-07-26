from pathlib import Path

import pytest

from app.cli.harness import (
    ProviderSmokeGateError,
    ProviderSmokeGateErrorCode,
    ProviderSmokeLaunchRequest,
    authorize_provider_smoke,
)


def launch_request(
    tmp_path: Path,
    **overrides: object,
) -> ProviderSmokeLaunchRequest:
    values: dict[str, object] = {
        "acknowledged": True,
        "provider": "openai",
        "model": "gpt-test",
        "max_output_tokens": 64,
        "timeout_seconds": 10,
        "cost_cap_microusd": 500,
        "config_path": tmp_path / "providers.json",
        "database_path": tmp_path / "smoke.sqlite3",
        "result_path": tmp_path / "result.json",
        "destination_url": "https://api.openai.example/v1",
        "environment_variable": "ATLAS_SMOKE_TEST_KEY",
    }
    values.update(overrides)
    return ProviderSmokeLaunchRequest.model_validate(values)


def test_explicit_positive_limits_authorize_openai_smoke(
    tmp_path: Path,
) -> None:
    authorized = authorize_provider_smoke(launch_request(tmp_path))

    assert authorized.acknowledged
    assert authorized.max_output_tokens == 64
    assert authorized.timeout_seconds == 10
    assert authorized.cost_cap_microusd == 500
    assert authorized.destination_url == "https://api.openai.example/v1"
    assert authorized.openrouter_policy_path is None


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        (
            {
                "acknowledged": False,
                "max_output_tokens": None,
                "timeout_seconds": None,
                "cost_cap_microusd": None,
            },
            ProviderSmokeGateErrorCode.ACKNOWLEDGEMENT,
        ),
        (
            {"max_output_tokens": None},
            ProviderSmokeGateErrorCode.TOKEN_CAP,
        ),
        (
            {"timeout_seconds": 61},
            ProviderSmokeGateErrorCode.TIMEOUT,
        ),
        (
            {"cost_cap_microusd": 0},
            ProviderSmokeGateErrorCode.COST_CAP,
        ),
        (
            {"environment_variable": "openai_api_key"},
            ProviderSmokeGateErrorCode.ENVIRONMENT,
        ),
        (
            {"destination_url": "http://api.example/v1"},
            ProviderSmokeGateErrorCode.ENDPOINT,
        ),
    ),
)
def test_live_smoke_gates_fail_in_deterministic_order(
    tmp_path: Path,
    overrides: dict[str, object],
    expected: ProviderSmokeGateErrorCode,
) -> None:
    with pytest.raises(ProviderSmokeGateError) as captured:
        authorize_provider_smoke(launch_request(tmp_path, **overrides))

    assert captured.value.code is expected
    assert str(captured.value) == "live provider smoke admission failed"


def test_paths_are_absolute_distinct_and_provider_policy_is_paired(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProviderSmokeGateError) as relative:
        authorize_provider_smoke(
            launch_request(tmp_path, result_path=Path("result.json"))
        )
    with pytest.raises(ProviderSmokeGateError) as duplicate:
        authorize_provider_smoke(
            launch_request(
                tmp_path,
                result_path=tmp_path / "providers.json",
            )
        )
    with pytest.raises(ProviderSmokeGateError) as missing_policy:
        authorize_provider_smoke(
            launch_request(tmp_path, provider="openrouter")
        )
    with pytest.raises(ProviderSmokeGateError) as unexpected_policy:
        authorize_provider_smoke(
            launch_request(
                tmp_path,
                openrouter_policy_path=tmp_path / "openrouter.json",
            )
        )

    assert relative.value.code is ProviderSmokeGateErrorCode.PATH
    assert duplicate.value.code is ProviderSmokeGateErrorCode.PATH
    assert missing_policy.value.code is ProviderSmokeGateErrorCode.POLICY
    assert unexpected_policy.value.code is ProviderSmokeGateErrorCode.POLICY


def test_openrouter_requires_private_policy_path_in_authorized_request(
    tmp_path: Path,
) -> None:
    authorized = authorize_provider_smoke(
        launch_request(
            tmp_path,
            provider="openrouter",
            destination_url="https://openrouter.example/api/v1",
            openrouter_policy_path=tmp_path / "openrouter.json",
        )
    )

    assert authorized.provider == "openrouter"
    assert authorized.openrouter_policy_path == tmp_path / "openrouter.json"
