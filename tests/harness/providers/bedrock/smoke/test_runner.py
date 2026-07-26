import asyncio
import hashlib
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from app.cli.harness import (
    BedrockSmokeBinding,
    BedrockSmokeGrantPayload,
    BedrockSmokeLaunchRequest,
    admit_bedrock_smoke,
    bedrock_smoke_model_sha256,
    run_bedrock_smoke,
    sign_bedrock_smoke_grant,
)
from app.services.harness.providers import (
    BedrockCredentialMaterial,
    BedrockIdentityReference,
    provider_destination_sha256,
)
from tests.harness.providers.bedrock.smoke.fixtures import (
    ENDPOINT,
    IDENTITY_ID,
    MODEL_ID,
    NOW,
    configuration,
    identity,
    route_policy,
    text_events,
    tool_events,
    write_private,
)

SIGNING_KEY = b"grant-key-canary-" + b"k" * 32
CREDENTIAL_CANARY = b"credential-canary"


class FakeCredentialBackend:
    def __init__(self) -> None:
        self.materials: list[BedrockCredentialMaterial] = []

    def load(
        self,
        identity: BedrockIdentityReference,
    ) -> BedrockCredentialMaterial:
        assert identity.identity_reference_id == IDENTITY_ID
        material = BedrockCredentialMaterial(
            bytearray(CREDENTIAL_CANARY),
            bytearray(b"secret-canary"),
            bytearray(b"session-canary"),
            expires_at=NOW + timedelta(minutes=5),
        )
        self.materials.append(material)
        return material


class FakeStream:
    def __init__(self, events: tuple[dict[str, object], ...]) -> None:
        self._events = iter(events)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self) -> dict[str, object]:
        return next(self._events)

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, events: tuple[dict[str, object], ...]) -> None:
        self.stream = FakeStream(events)
        self.closed = False
        self.request: dict[str, object] | None = None

    def converse_stream(self, **request: object) -> dict[str, object]:
        self.request = request
        return {
            "stream": self.stream,
            "ResponseMetadata": {"RequestId": "redacted-by-hash"},
        }

    def close(self) -> None:
        self.closed = True


class FakeClientFactory:
    def __init__(self) -> None:
        self.clients = [
            FakeClient(text_events()),
            FakeClient(tool_events()),
        ]
        self.calls = 0

    def create(
        self,
        route,
        credential: BedrockCredentialMaterial,
        *,
        timeout_seconds: int,
    ) -> FakeClient:
        assert route.model_id == MODEL_ID
        assert bytes(credential.views()[0]) == CREDENTIAL_CANARY
        assert timeout_seconds == 30
        client = self.clients[self.calls]
        self.calls += 1
        return client


def test_bedrock_smoke_runs_two_cost_accounted_calls_and_redacts_evidence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        inputs = _write_inputs(tmp_path)
        launch = admit_bedrock_smoke(
            BedrockSmokeLaunchRequest(
                acknowledged=True,
                grant_path=inputs["grant"],
                configuration_path=inputs["configuration"],
                route_policy_path=inputs["policy"],
                identity_path=inputs["identity"],
                database_path=tmp_path / "evidence.sqlite3",
                result_path=tmp_path / "result.json",
                signing_key_environment_variable="ATLAS_BEDROCK_GRANT_KEY",
            )
        )
        backend = FakeCredentialBackend()
        clients = FakeClientFactory()

        result = await run_bedrock_smoke(
            launch,
            environment={b"ATLAS_BEDROCK_GRANT_KEY": SIGNING_KEY},
            credential_backend=backend,
            client_factory=clients,
            clock=lambda: NOW,
        )

        assert clients.calls == 2
        assert len(backend.materials) == 3
        assert all(
            bytes(material.views()[0]) == bytes(len(CREDENTIAL_CANARY))
            for material in backend.materials
        )
        assert all(client.closed for client in clients.clients)
        assert all(client.stream.closed for client in clients.clients)
        assert clients.clients[0].request is not None
        assert clients.clients[0].request["modelId"] == MODEL_ID
        tool_request = clients.clients[1].request
        assert tool_request is not None
        assert "toolConfig" in tool_request
        assert tool_request["toolConfig"]["toolChoice"] == {
            "tool": {"name": "atlas_smoke_report"}
        }
        assert result.provider_calls == 2
        assert result.cancellation_verified
        assert result.text_verified
        assert result.tool_verified
        assert result.charged_cost_microusd == 29
        assert result.signed_cost_cap_microusd == 20_000
        assert result.active_cost_reservations == 0
        result_content = launch.result_path.read_bytes()
        database_content = launch.database_path.read_bytes()
        for canary in (
            SIGNING_KEY,
            CREDENTIAL_CANARY,
            b"ATLAS_SMOKE_OK",
            b"redacted-by-hash",
        ):
            assert canary not in result_content
            assert canary not in database_content
        assert stat.S_IMODE(launch.result_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(launch.database_path.stat().st_mode) == 0o600

    asyncio.run(scenario())


def test_insufficient_signed_cost_cap_fails_before_credentials_or_call(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        inputs = _write_inputs(tmp_path, call_cap_microusd=1)
        database_path = tmp_path / "evidence.sqlite3"
        result_path = tmp_path / "result.json"
        launch = admit_bedrock_smoke(
            BedrockSmokeLaunchRequest(
                acknowledged=True,
                grant_path=inputs["grant"],
                configuration_path=inputs["configuration"],
                route_policy_path=inputs["policy"],
                identity_path=inputs["identity"],
                database_path=database_path,
                result_path=result_path,
                signing_key_environment_variable="ATLAS_BEDROCK_GRANT_KEY",
            )
        )
        backend = FakeCredentialBackend()
        clients = FakeClientFactory()

        with pytest.raises(ValueError, match="cost cap"):
            await run_bedrock_smoke(
                launch,
                environment={b"ATLAS_BEDROCK_GRANT_KEY": SIGNING_KEY},
                credential_backend=backend,
                client_factory=clients,
                clock=lambda: NOW,
            )

        assert not backend.materials
        assert clients.calls == 0
        assert not database_path.exists()
        assert not result_path.exists()

    asyncio.run(scenario())


def _write_inputs(
    tmp_path: Path,
    *,
    call_cap_microusd: int = 10_000,
) -> dict[str, Path]:
    provider_configuration = configuration()
    configuration_content = provider_configuration.model_dump_json()
    configuration_path = tmp_path / "providers.json"
    write_private(configuration_path, configuration_content)
    policy = route_policy()
    identity_reference = identity(tmp_path)
    binding = BedrockSmokeBinding(
        configuration_sha256=hashlib.sha256(
            configuration_content.encode()
        ).hexdigest(),
        route_policy_sha256=bedrock_smoke_model_sha256(policy),
        identity_sha256=bedrock_smoke_model_sha256(identity_reference),
        identity_reference_id=IDENTITY_ID,
        aws_account_id="123456789012",
        model_id=MODEL_ID,
        region="us-east-1",
        destination_sha256=provider_destination_sha256(ENDPOINT),
    )
    grant = sign_bedrock_smoke_grant(
        BedrockSmokeGrantPayload(
            authorization_id="awsg_" + "a" * 32,
            key_id="test-key",
            binding=binding,
            timeout_seconds=30,
            text_max_output_tokens=16,
            tool_max_output_tokens=16,
            text_cost_cap_microusd=call_cap_microusd,
            tool_cost_cap_microusd=call_cap_microusd,
            total_cost_cap_microusd=call_cap_microusd * 2,
            issued_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=5),
        ),
        SIGNING_KEY,
    )
    paths = {
        "configuration": configuration_path,
        "policy": tmp_path / "route-policy.json",
        "identity": tmp_path / "identity.json",
        "grant": tmp_path / "grant.json",
    }
    write_private(paths["policy"], policy.model_dump_json())
    write_private(paths["identity"], identity_reference.model_dump_json())
    write_private(paths["grant"], grant.model_dump_json())
    return paths
