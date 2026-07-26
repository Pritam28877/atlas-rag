import pytest
from pydantic import ValidationError

from app.services.harness.providers.egress_policy import (
    provider_destination_sha256,
)
from app.services.harness.providers.vertex_identity import (
    VertexCredentialSourceKind,
)
from app.services.harness.providers.vertex_policy import (
    authorize_vertex_route,
)
from tests.harness.providers.vertex.fixtures import (
    ENDPOINT,
    MODEL_ID,
    configuration,
    external_account_path,
    identity,
    route_policy,
)


def test_identity_sources_require_exact_non_secret_fields() -> None:
    application_default = identity()
    external_account = identity(
        VertexCredentialSourceKind.EXTERNAL_ACCOUNT_FILE,
        external_account_file=external_account_path(),
        external_account_file_sha256="9" * 64,
    )
    impersonated = identity(
        VertexCredentialSourceKind.IMPERSONATED_SERVICE_ACCOUNT,
        target_service_account=(
            "atlas-runtime@atlas-test-12345.iam.gserviceaccount.com"
        ),
    )

    assert application_default.external_account_file is None
    assert external_account.external_account_file == external_account_path()
    assert impersonated.target_service_account is not None
    with pytest.raises(ValidationError, match="inconsistent"):
        identity(
            VertexCredentialSourceKind.APPLICATION_DEFAULT,
            external_account_file=external_account_path(),
            external_account_file_sha256="9" * 64,
        )
    with pytest.raises(ValidationError, match="inconsistent"):
        identity(
            VertexCredentialSourceKind.EXTERNAL_ACCOUNT_FILE,
            external_account_file=external_account_path().relative_to("/"),
            external_account_file_sha256="9" * 64,
        )


def test_route_authorization_binds_project_location_model_and_identity() -> None:
    loaded, route = configuration()

    authorized = authorize_vertex_route(
        loaded,
        route,
        route_policy(),
        identity(),
    )

    assert authorized.location == "us-central1"
    assert authorized.model_id == MODEL_ID
    assert authorized.endpoint_url == ENDPOINT
    assert authorized.stream_url == (
        "https://us-central1-aiplatform.googleapis.com/v1/"
        "projects/atlas-test-12345/locations/us-central1/"
        "publishers/google/models/gemini-2.5-flash:streamGenerateContent"
    )


def test_endpoint_location_and_global_substitution_fail_closed() -> None:
    substituted_endpoint = "https://europe-west1-aiplatform.googleapis.com/"
    with pytest.raises(ValidationError, match="location"):
        route_policy(
            endpoint_url=substituted_endpoint,
            destination_sha256=provider_destination_sha256(
                substituted_endpoint
            ),
        )
    with pytest.raises(ValidationError, match="regional"):
        route_policy(
            location="global",
            endpoint_url="https://global-aiplatform.googleapis.com/",
            destination_sha256=provider_destination_sha256(
                "https://global-aiplatform.googleapis.com/"
            ),
            allowed_locations=("global",),
        )
    with pytest.raises(ValidationError, match="exactly one"):
        route_policy(
            allowed_locations=("europe-west1", "us-central1"),
        )


def test_project_and_credential_substitution_fail_closed() -> None:
    loaded, route = configuration()
    substituted_project = identity().model_copy(
        update={"quota_project_id": "other-project-12345"}
    )
    substituted_handle = identity().model_copy(
        update={"credential_handle": "pcr_" + "9" * 32}
    )

    for substituted_identity in (substituted_project, substituted_handle):
        with pytest.raises(ValueError, match="authorization"):
            authorize_vertex_route(
                loaded,
                route,
                route_policy(),
                substituted_identity,
            )
