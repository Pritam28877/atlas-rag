import pytest
from pydantic import ValidationError

from app.services.harness.providers.local_compatible_identity import (
    LocalAuthenticationMode,
)
from app.services.harness.providers.local_compatible_policy import (
    LocalEndpointMode,
    authorize_local_compatible_route,
    canonical_local_compatible_url,
    local_destination_sha256,
)
from tests.harness.providers.local_compatible.fixtures import (
    ENDPOINT,
    configuration,
    identity,
    route_policy,
)


def test_no_auth_and_bearer_references_never_contain_a_token() -> None:
    no_auth = identity()
    bearer = identity(
        LocalAuthenticationMode.BEARER_ENVIRONMENT,
        bearer_environment_variable="LOCAL_MODEL_TOKEN",
    )

    assert no_auth.bearer_environment_variable is None
    assert bearer.bearer_environment_variable == "LOCAL_MODEL_TOKEN"
    with pytest.raises(ValidationError, match="inconsistent"):
        identity(
            LocalAuthenticationMode.NONE,
            bearer_environment_variable="LOCAL_MODEL_TOKEN",
        )
    with pytest.raises(ValidationError, match="inconsistent"):
        identity(LocalAuthenticationMode.BEARER_ENVIRONMENT)


def test_loopback_route_binds_exact_endpoint_model_and_identity() -> None:
    loaded, route = configuration()

    authorized = authorize_local_compatible_route(
        loaded,
        route,
        route_policy(),
        identity(),
    )

    assert authorized.endpoint_url == ENDPOINT
    assert authorized.responses_url == f"{ENDPOINT}responses"
    assert authorized.endpoint_mode is LocalEndpointMode.LOOPBACK
    assert authorized.authentication is LocalAuthenticationMode.NONE


def test_plaintext_and_ip_endpoints_are_loopback_only() -> None:
    for endpoint in (
        "http://localhost:11434/v1/",
        "http://10.0.0.5:11434/v1/",
        "https://10.0.0.5:11434/v1/",
        "http://127.0.0.1/v1/",
    ):
        with pytest.raises(ValueError):
            canonical_local_compatible_url(endpoint)

    assert canonical_local_compatible_url(
        "http://[::1]:11434/v1/"
    ) == "http://[::1]:11434/v1/"
    assert canonical_local_compatible_url(
        "http://127.0.0.1:80/v1/"
    ) == "http://127.0.0.1:80/v1/"


def test_remote_endpoint_requires_tls_and_exact_allowlist() -> None:
    endpoint = "https://models.internal.example/v1/"
    destination = local_destination_sha256(endpoint)
    remote = route_policy(
        endpoint_mode=LocalEndpointMode.REMOTE_TLS,
        endpoint_url=endpoint,
        destination_sha256=destination,
        allowed_remote_hostnames=("models.internal.example",),
    )

    assert remote.responses_url() == f"{endpoint}responses"
    with pytest.raises(ValidationError, match="mode"):
        route_policy(
            endpoint_mode=LocalEndpointMode.REMOTE_TLS,
            endpoint_url=endpoint,
            destination_sha256=destination,
            allowed_remote_hostnames=("other.internal.example",),
        )
    with pytest.raises(ValueError, match="TLS"):
        canonical_local_compatible_url(
            "http://models.internal.example:8080/v1/"
        )


def test_endpoint_and_credential_substitution_fail_closed() -> None:
    loaded, route = configuration()
    substituted = identity().model_copy(
        update={"credential_handle": "pcr_" + "9" * 32}
    )

    with pytest.raises(ValueError, match="authorization"):
        authorize_local_compatible_route(
            loaded,
            route,
            route_policy(),
            substituted,
        )
    with pytest.raises(ValidationError, match="hash"):
        route_policy(destination_sha256="9" * 64)
