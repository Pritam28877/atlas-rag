from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.cli.harness import (
    BedrockSmokeBinding,
    BedrockSmokeGateError,
    BedrockSmokeGateErrorCode,
    BedrockSmokeGrantPayload,
    BedrockSmokeLaunchRequest,
    admit_bedrock_smoke,
    sign_bedrock_smoke_grant,
    verify_bedrock_smoke_grant,
)

NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)
SIGNING_KEY = b"s" * 32


def test_signed_grant_authorizes_exact_two_call_budget(tmp_path: Path) -> None:
    launch = admit_bedrock_smoke(launch_request(tmp_path))
    payload = grant_payload()
    signed = sign_bedrock_smoke_grant(payload, SIGNING_KEY)

    authorized = verify_bedrock_smoke_grant(
        launch,
        signed,
        SIGNING_KEY,
        binding(),
        observed_at=NOW,
    )

    assert authorized.grant.maximum_provider_calls == 2
    assert authorized.grant.total_cost_cap_microusd == 100
    assert authorized.grant.binding.aws_account_id == "123456789012"


@pytest.mark.parametrize(
    ("request_update", "expected"),
    (
        ({"acknowledged": False}, BedrockSmokeGateErrorCode.ACKNOWLEDGEMENT),
        (
            {"signing_key_environment_variable": "unsafe_key"},
            BedrockSmokeGateErrorCode.ENVIRONMENT,
        ),
        (
            {"result_path": Path("relative.json")},
            BedrockSmokeGateErrorCode.PATH,
        ),
    ),
)
def test_launch_admission_fails_before_any_live_work(
    tmp_path: Path,
    request_update: dict[str, object],
    expected: BedrockSmokeGateErrorCode,
) -> None:
    with pytest.raises(BedrockSmokeGateError) as captured:
        admit_bedrock_smoke(launch_request(tmp_path, **request_update))

    assert captured.value.code is expected
    assert str(captured.value) == "Bedrock live smoke admission failed"


def test_signature_expiry_and_binding_fail_closed(tmp_path: Path) -> None:
    launch = admit_bedrock_smoke(launch_request(tmp_path))
    signed = sign_bedrock_smoke_grant(grant_payload(), SIGNING_KEY)
    tampered = signed.model_copy(
        update={
            "payload": signed.payload.model_copy(
                update={"text_cost_cap_microusd": 51}
            )
        }
    )

    with pytest.raises(BedrockSmokeGateError) as signature:
        verify_bedrock_smoke_grant(
            launch,
            tampered,
            SIGNING_KEY,
            binding(),
            observed_at=NOW,
        )
    with pytest.raises(BedrockSmokeGateError) as expiry:
        verify_bedrock_smoke_grant(
            launch,
            signed,
            SIGNING_KEY,
            binding(),
            observed_at=NOW + timedelta(hours=1),
        )
    changed_binding = binding().model_copy(
        update={"destination_sha256": "9" * 64}
    )
    with pytest.raises(BedrockSmokeGateError) as binding_error:
        verify_bedrock_smoke_grant(
            launch,
            signed,
            SIGNING_KEY,
            changed_binding,
            observed_at=NOW,
        )

    assert signature.value.code is BedrockSmokeGateErrorCode.SIGNATURE
    assert expiry.value.code is BedrockSmokeGateErrorCode.EXPIRY
    assert binding_error.value.code is BedrockSmokeGateErrorCode.BINDING


def launch_request(
    tmp_path: Path,
    **updates: object,
) -> BedrockSmokeLaunchRequest:
    values = {
        "acknowledged": True,
        "grant_path": tmp_path / "grant.json",
        "configuration_path": tmp_path / "providers.json",
        "route_policy_path": tmp_path / "route-policy.json",
        "identity_path": tmp_path / "identity.json",
        "database_path": tmp_path / "evidence.sqlite3",
        "result_path": tmp_path / "result.json",
        "signing_key_environment_variable": "ATLAS_BEDROCK_GRANT_KEY",
        **updates,
    }
    return BedrockSmokeLaunchRequest.model_validate(values)


def grant_payload() -> BedrockSmokeGrantPayload:
    return BedrockSmokeGrantPayload(
        authorization_id="awsg_" + "a" * 32,
        key_id="test-key",
        binding=binding(),
        timeout_seconds=30,
        text_max_output_tokens=16,
        tool_max_output_tokens=16,
        text_cost_cap_microusd=50,
        tool_cost_cap_microusd=50,
        total_cost_cap_microusd=100,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=59),
    )


def binding() -> BedrockSmokeBinding:
    return BedrockSmokeBinding(
        configuration_sha256="1" * 64,
        route_policy_sha256="2" * 64,
        identity_sha256="3" * 64,
        identity_reference_id="awsid_" + "4" * 32,
        aws_account_id="123456789012",
        model_id="amazon.nova-lite-v1:0",
        region="us-east-1",
        destination_sha256="5" * 64,
    )
