from pathlib import Path

import pytest

from app.services.harness.providers import (
    AuthorizedBedrockRoute,
    BedrockCredentialMaterial,
    BedrockCredentialResolutionError,
    BedrockCredentialSourceKind,
    BedrockIdentityReference,
    BotocoreNetworkCredentialBackend,
)
from app.services.harness.providers.egress_policy import (
    provider_destination_sha256,
)

ENDPOINT = "https://bedrock-runtime.us-east-1.amazonaws.com/"


class RecordingLoader:
    def __init__(self) -> None:
        self.calls: list[tuple[BedrockIdentityReference, AuthorizedBedrockRoute]] = []

    def __call__(
        self,
        identity: BedrockIdentityReference,
        route: AuthorizedBedrockRoute,
    ) -> BedrockCredentialMaterial:
        self.calls.append((identity, route))
        return BedrockCredentialMaterial(
            bytearray(b"access"),
            bytearray(b"secret"),
            bytearray(b"token"),
            expires_at=None,
        )


@pytest.mark.parametrize(
    "source",
    (
        BedrockCredentialSourceKind.WEB_IDENTITY,
        BedrockCredentialSourceKind.INSTANCE_METADATA,
    ),
)
def test_network_backend_invokes_only_selected_bound_source(
    source: BedrockCredentialSourceKind,
) -> None:
    web_loader = RecordingLoader()
    instance_loader = RecordingLoader()
    route = _route()
    backend = BotocoreNetworkCredentialBackend(
        route,
        web_identity_loader=web_loader,
        instance_metadata_loader=instance_loader,
    )
    identity = _identity(source)

    material = backend.load(identity)

    expected_web_calls = 1 if source is BedrockCredentialSourceKind.WEB_IDENTITY else 0
    expected_instance_calls = (
        1 if source is BedrockCredentialSourceKind.INSTANCE_METADATA else 0
    )
    assert len(web_loader.calls) == expected_web_calls
    assert len(instance_loader.calls) == expected_instance_calls
    assert (web_loader.calls or instance_loader.calls)[0] == (
        identity,
        route,
    )
    material.zero()


def test_network_backend_rejects_identity_and_static_source_substitution() -> None:
    backend = BotocoreNetworkCredentialBackend(
        _route(),
        web_identity_loader=RecordingLoader(),
        instance_metadata_loader=RecordingLoader(),
    )
    substituted = _identity(BedrockCredentialSourceKind.INSTANCE_METADATA).model_copy(
        update={"credential_handle": "pcr_" + "9" * 32}
    )

    with pytest.raises(BedrockCredentialResolutionError):
        backend.load(substituted)
    with pytest.raises(BedrockCredentialResolutionError):
        backend.load(
            BedrockIdentityReference(
                identity_reference_id="awsid_" + "1" * 32,
                credential_handle="pcr_" + "2" * 32,
                source=BedrockCredentialSourceKind.ENVIRONMENT,
            )
        )


def _identity(
    source: BedrockCredentialSourceKind,
) -> BedrockIdentityReference:
    values: dict[str, object] = {
        "identity_reference_id": "awsid_" + "1" * 32,
        "credential_handle": "pcr_" + "2" * 32,
        "source": source,
    }
    if source is BedrockCredentialSourceKind.WEB_IDENTITY:
        values.update(
            {
                "role_arn": ("arn:aws:iam::123456789012:role/atlas-bedrock"),
                "web_identity_token_file": Path("/var/run/secrets/atlas-token"),
                "role_session_name": "atlas-session",
            }
        )
    return BedrockIdentityReference.model_validate(values)


def _route() -> AuthorizedBedrockRoute:
    return AuthorizedBedrockRoute(
        route_id="bedrock.primary",
        model_id="amazon.nova-lite-v1:0",
        region="us-east-1",
        credential_handle="pcr_" + "2" * 32,
        identity_reference_id="awsid_" + "1" * 32,
        endpoint_url=ENDPOINT,
        destination_sha256=provider_destination_sha256(ENDPOINT),
        allowed_inference_regions=("us-east-1",),
        cross_region_inference=False,
    )
