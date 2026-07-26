import app.services.harness.providers.bedrock_boto_client as bedrock_boto_client
from app.services.harness.providers import (
    AuthorizedBedrockRoute,
    BedrockCredentialMaterial,
    Boto3BedrockRuntimeClientFactory,
    response_event_stream,
)
from app.services.harness.providers.egress_policy import (
    provider_destination_sha256,
)

ENDPOINT = "https://bedrock-runtime.us-east-1.amazonaws.com/"


class Client:
    def converse_stream(self, **request: object) -> dict[str, object]:
        return request

    def close(self) -> None:
        return None


class Stream:
    def __iter__(self):
        return self

    def __next__(self) -> dict[str, object]:
        raise StopIteration

    def close(self) -> None:
        return None


class Config:
    def __init__(self, **arguments: object) -> None:
        self.__dict__.update(arguments)


def test_factory_uses_only_bound_route_credentials_and_zero_retries(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    client = Client()

    def create_client(service_name: str, **arguments: object) -> Client:
        captured["service_name"] = service_name
        captured.update(arguments)
        return client

    monkeypatch.setattr(
        bedrock_boto_client,
        "_load_boto3_factories",
        lambda: (create_client, Config),
    )
    credential = BedrockCredentialMaterial(
        bytearray(b"access-canary"),
        bytearray(b"secret-canary"),
        bytearray(b"token-canary"),
        expires_at=None,
    )

    created = Boto3BedrockRuntimeClientFactory().create(
        _route(),
        credential,
        timeout_seconds=20,
    )

    assert created is client
    assert captured["service_name"] == "bedrock-runtime"
    assert captured["region_name"] == "us-east-1"
    assert captured["endpoint_url"] == ENDPOINT
    assert captured["aws_access_key_id"] == "access-canary"
    assert captured["aws_secret_access_key"] == "secret-canary"
    assert captured["aws_session_token"] == "token-canary"
    config = captured["config"]
    assert config.connect_timeout == 10
    assert config.read_timeout == 20
    assert config.retries["max_attempts"] == 0
    assert config.max_pool_connections == 1
    credential.zero()


def test_response_envelope_hashes_metadata_and_rejects_unknown_fields() -> None:
    stream = Stream()

    envelope = response_event_stream(
        {
            "stream": stream,
            "ResponseMetadata": {"RequestId": "request-1"},
        }
    )

    assert envelope.stream is stream
    assert envelope.response_metadata_sha256 is not None
    try:
        response_event_stream({"stream": stream, "unexpected": "value"})
    except ValueError as error:
        assert "envelope" in str(error)
    else:
        raise AssertionError("unknown Bedrock response field was accepted")


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
