import pytest
from pydantic import ValidationError

from app.services.harness.protocol import DataClassification
from app.services.harness.providers import (
    AuthorizedEgressTarget,
    ProviderEgressPolicy,
    ProviderEgressPolicyError,
    ProviderEgressPolicyErrorCode,
    authorize_egress_target,
    canonical_provider_origin,
    canonical_provider_url,
    provider_destination_sha256,
)

DESTINATION_URL = "https://api.provider.example/v1"


def policy(**overrides: object) -> ProviderEgressPolicy:
    values: dict[str, object] = {
        "provider": "configured-provider",
        "destination_url": DESTINATION_URL,
        "destination_sha256": provider_destination_sha256(
            DESTINATION_URL
        ),
        "allowed_redirect_origins": (
            "https://uploads.provider.example",
        ),
        "accepted_classifications": (
            DataClassification.CONFIDENTIAL,
            DataClassification.INTERNAL,
        ),
        "max_request_bytes": 4_096,
        "max_response_bytes": 8_192,
        "max_redirects": 2,
    }
    values.update(overrides)
    return ProviderEgressPolicy.model_validate(values)


def authorize(
    *,
    configured_policy: ProviderEgressPolicy | None = None,
    target_url: str = "https://api.provider.example/v1/responses",
    classification: DataClassification = DataClassification.CONFIDENTIAL,
    request_bytes: int = 100,
    resolved_addresses: tuple[str, ...] = (
        "1.1.1.1",
        "2606:4700:4700::1111",
    ),
    redirect_count: int = 0,
) -> AuthorizedEgressTarget:
    return authorize_egress_target(
        configured_policy or policy(),
        target_url=target_url,
        classification=classification,
        request_bytes=request_bytes,
        resolved_addresses=resolved_addresses,
        redirect_count=redirect_count,
    )


def test_authorization_returns_canonical_public_resolution_evidence() -> None:
    target = authorize(
        target_url="HTTPS://API.PROVIDER.EXAMPLE./v1/responses"
    )

    assert target.canonical_url == (
        "https://api.provider.example/v1/responses"
    )
    assert target.destination_sha256 == provider_destination_sha256(
        DESTINATION_URL
    )
    assert target.hostname == "api.provider.example"
    assert target.port == 443
    assert target.resolved_addresses == (
        "1.1.1.1",
        "2606:4700:4700::1111",
    )


@pytest.mark.parametrize(
    ("target_url", "expected_code"),
    [
        (
            "https://evil.example/v1/responses",
            ProviderEgressPolicyErrorCode.DESTINATION,
        ),
        (
            "https://api.provider.example/v10/responses",
            ProviderEgressPolicyErrorCode.DESTINATION,
        ),
        (
            "http://api.provider.example/v1/responses",
            ProviderEgressPolicyErrorCode.URL,
        ),
        (
            "https://user@api.provider.example/v1/responses",
            ProviderEgressPolicyErrorCode.URL,
        ),
        (
            "https://api.provider.example/v1?key=secret",
            ProviderEgressPolicyErrorCode.URL,
        ),
        (
            "https://127.0.0.1/v1/responses",
            ProviderEgressPolicyErrorCode.URL,
        ),
        (
            "https://[::1]/v1/responses",
            ProviderEgressPolicyErrorCode.URL,
        ),
        (
            "https://api_provider.example/v1/responses",
            ProviderEgressPolicyErrorCode.URL,
        ),
        (
            "https://api.provider.example:0/v1/responses",
            ProviderEgressPolicyErrorCode.URL,
        ),
        (
            "https://api.provider.example/v1/%2e%2e/private",
            ProviderEgressPolicyErrorCode.URL,
        ),
        (
            "https://api.provider.example/v1//responses",
            ProviderEgressPolicyErrorCode.URL,
        ),
    ],
)
def test_destination_and_ambiguous_url_bypasses_fail_closed(
    target_url: str,
    expected_code: ProviderEgressPolicyErrorCode,
) -> None:
    with pytest.raises(ProviderEgressPolicyError) as captured:
        authorize(target_url=target_url)

    assert captured.value.code is expected_code


def test_redirects_require_count_and_exact_canonical_origin() -> None:
    with pytest.raises(ProviderEgressPolicyError) as initial_cross_origin:
        authorize(
            target_url="https://uploads.provider.example/upload",
            redirect_count=0,
        )
    with pytest.raises(ProviderEgressPolicyError) as unapproved_redirect:
        authorize(
            target_url="https://evil.example/upload",
            redirect_count=1,
        )
    with pytest.raises(ProviderEgressPolicyError) as excess_redirect:
        authorize(
            target_url="https://uploads.provider.example/upload",
            redirect_count=3,
        )

    approved = authorize(
        target_url="https://uploads.provider.example/upload",
        redirect_count=1,
    )

    assert initial_cross_origin.value.code is (
        ProviderEgressPolicyErrorCode.DESTINATION
    )
    assert unapproved_redirect.value.code is (
        ProviderEgressPolicyErrorCode.REDIRECT
    )
    assert excess_redirect.value.code is ProviderEgressPolicyErrorCode.REDIRECT
    assert approved.redirect_count == 1


@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        ("10.0.0.1",),
        ("169.254.169.254",),
        ("0.0.0.0",),
        ("224.0.0.1",),
        ("::1",),
        ("fc00::1",),
        ("fe80::1",),
        ("1.1.1.1", "10.0.0.1"),
        ("1.1.1.1", "1.1.1.1"),
        (),
    ],
)
def test_private_mixed_or_duplicate_dns_answers_fail_closed(
    addresses: tuple[str, ...],
) -> None:
    with pytest.raises(ProviderEgressPolicyError) as captured:
        authorize(resolved_addresses=addresses)

    assert captured.value.code is ProviderEgressPolicyErrorCode.ADDRESS


def test_classification_and_request_size_are_enforced_before_transport() -> None:
    with pytest.raises(ProviderEgressPolicyError) as classification:
        authorize(classification=DataClassification.RESTRICTED)
    with pytest.raises(ProviderEgressPolicyError) as empty:
        authorize(request_bytes=0)
    with pytest.raises(ProviderEgressPolicyError) as oversized:
        authorize(request_bytes=4_097)

    assert classification.value.code is (
        ProviderEgressPolicyErrorCode.CLASSIFICATION
    )
    assert empty.value.code is ProviderEgressPolicyErrorCode.REQUEST_SIZE
    assert oversized.value.code is ProviderEgressPolicyErrorCode.REQUEST_SIZE


def test_policy_configuration_is_canonical_hash_bound_and_bounded() -> None:
    assert canonical_provider_url(
        "HTTPS://API.PROVIDER.EXAMPLE./v1"
    ) == DESTINATION_URL
    assert canonical_provider_origin(
        "https://api.provider.example"
    ) == "https://api.provider.example"

    with pytest.raises(ValidationError, match="must be canonical"):
        policy(destination_url="HTTPS://API.PROVIDER.EXAMPLE./v1")
    with pytest.raises(ValidationError, match="hash mismatch"):
        policy(destination_sha256="0" * 64)
    with pytest.raises(ValidationError, match="unique and sorted"):
        policy(
            allowed_redirect_origins=(
                "https://uploads.provider.example",
                "https://uploads.provider.example",
            )
        )
    with pytest.raises(ValidationError, match="require an allowed origin"):
        policy(allowed_redirect_origins=(), max_redirects=1)
