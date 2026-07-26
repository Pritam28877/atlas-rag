import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.cli.harness import (
    ProviderSmokeLaunchRequest,
    authorize_provider_smoke,
    run_provider_smoke,
)
from app.services.harness.protocol import (
    DataClassification,
    ProviderDataPolicy,
    ProviderModality,
    ProviderModelCapabilities,
    ProviderPriceRecord,
)
from app.services.harness.providers import (
    ProviderConfiguration,
    ProviderCredentialBinding,
    ProviderEgressRequest,
    ProviderEgressResponse,
    ProviderRouteConfiguration,
    provider_destination_sha256,
)
from app.services.harness.providers.credential_material import CredentialLease
from app.services.harness.providers.egress_policy import AuthorizedEgressTarget

DESTINATION = "https://provider.example/v1"
CANARY = b"provider-smoke-canary"
MODEL_BY_PROVIDER = {
    "openai": "gpt-smoke",
    "openrouter": "openai/gpt-smoke",
}


class SmokeConnector:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.calls = 0
        self.credential: CredentialLease | None = None

    async def send(
        self,
        request: ProviderEgressRequest,
        target: AuthorizedEgressTarget,
        credential: CredentialLease | None,
        *,
        cancellation,
        deadline_at,
        max_response_bytes: int,
    ) -> ProviderEgressResponse:
        self.calls += 1
        self.credential = credential
        assert credential is not None
        assert bytes(credential.secret_view()) == CANARY
        assert target.canonical_url == f"{DESTINATION}/responses"
        assert not cancellation.is_set()
        assert deadline_at > datetime.now(UTC)
        request_body = json.loads(request.body())
        assert request_body["stream"] is True
        assert request_body["store"] is False
        assert request_body["input"][0]["content"][0]["text"] == (
            "Reply with exactly ATLAS_SMOKE_OK."
        )
        response_body = _response_body(self.provider)
        assert len(response_body) <= max_response_bytes
        return ProviderEgressResponse(
            status=200,
            headers=(),
            body=response_body,
        )


def test_openai_and_openrouter_smokes_run_through_audited_runtime(
    tmp_path: Path,
) -> None:
    async def scenario(provider: str) -> None:
        run_directory = tmp_path / provider
        run_directory.mkdir(mode=0o700)
        model = MODEL_BY_PROVIDER[provider]
        config_path = run_directory / "providers.json"
        database_path = run_directory / "smoke.sqlite3"
        result_path = run_directory / "result.json"
        _write_private(config_path, _configuration(provider, model))
        policy_path = None
        if provider == "openrouter":
            policy_path = run_directory / "openrouter-policy.json"
            _write_private(
                policy_path,
                json.dumps(
                    {
                        "order": ["approved-endpoint"],
                        "only": ["approved-endpoint"],
                        "allow_fallbacks": False,
                        "require_parameters": True,
                        "data_collection": "deny",
                        "zdr": True,
                    },
                    separators=(",", ":"),
                ),
            )
        authorized = authorize_provider_smoke(
            ProviderSmokeLaunchRequest(
                acknowledged=True,
                provider=provider,
                model=model,
                max_output_tokens=16,
                timeout_seconds=10,
                cost_cap_microusd=50,
                config_path=config_path,
                database_path=database_path,
                result_path=result_path,
                destination_url=DESTINATION,
                environment_variable="ATLAS_PROVIDER_SMOKE_KEY",
                openrouter_policy_path=policy_path,
            )
        )
        connector = SmokeConnector(provider)

        result = await run_provider_smoke(
            authorized,
            connector=connector,
            lookup=_public_lookup,
            environment={b"ATLAS_PROVIDER_SMOKE_KEY": CANARY},
        )

        assert connector.calls == 1
        assert connector.credential is not None
        assert connector.credential.released
        assert result.output_verified
        assert result.charged_cost_microusd == 50
        assert result.audit_events == 2
        assert result.active_credential_leases == 0
        assert (result.routing_metadata_sha256 is not None) == (
            provider == "openrouter"
        )
        assert stat.S_IMODE(result_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
        persisted = result_path.read_bytes()
        database = database_path.read_bytes()
        assert CANARY not in persisted
        assert CANARY not in database
        assert b"ATLAS_SMOKE_OK" not in persisted
        assert b"ATLAS_SMOKE_OK" not in database

    import asyncio

    asyncio.run(scenario("openai"))
    asyncio.run(scenario("openrouter"))


async def _public_lookup(
    hostname: str,
    port: int,
) -> tuple[str, ...]:
    assert hostname == "provider.example"
    assert port == 443
    return ("1.1.1.1",)


def _configuration(provider: str, model: str) -> str:
    now = datetime.now(UTC)
    destination_sha256 = provider_destination_sha256(DESTINATION)
    model_revision = "1" * 64
    policy_revision = "2" * 64
    price_revision = "3" * 64
    credential_handle = "pcr_" + "4" * 32
    configuration = ProviderConfiguration(
        schema_version=1,
        models=(
            ProviderModelCapabilities(
                provider=provider,
                model=model,
                model_revision_sha256=model_revision,
                catalog_snapshot_sha256="5" * 64,
                capabilities=(),
                input_modalities=(ProviderModality.TEXT,),
                output_modalities=(ProviderModality.TEXT,),
                context_features=(),
                available_regions=("global",),
                context_window_tokens=8_192,
                max_output_tokens=4_096,
                observed_at=now,
            ),
        ),
        credential_bindings=(
            ProviderCredentialBinding(
                handle=credential_handle,
                provider=provider,
                destination_sha256=destination_sha256,
            ),
        ),
        data_policies=(
            ProviderDataPolicy(
                provider=provider,
                policy_revision_sha256=policy_revision,
                destination_sha256=destination_sha256,
                accepted_classifications=(DataClassification.PUBLIC,),
                allowed_regions=("global",),
                retention_days=0,
                training_enabled=False,
                observed_at=now,
            ),
        ),
        prices=(
            ProviderPriceRecord(
                provider=provider,
                model=model,
                model_revision_sha256=model_revision,
                region="global",
                price_version_sha256=price_revision,
                input_microusd_per_million_tokens=1,
                cached_input_microusd_per_million_tokens=1,
                output_microusd_per_million_tokens=1,
                reasoning_microusd_per_million_tokens=1,
                effective_at=now - timedelta(days=1),
            ),
        ),
        routes=(
            ProviderRouteConfiguration(
                route_id=f"{provider}.smoke",
                provider=provider,
                model=model,
                model_revision_sha256=model_revision,
                region="global",
                credential_handle=credential_handle,
                policy_revision_sha256=policy_revision,
                price_version_sha256=price_revision,
                priority=1,
                enabled=True,
            ),
        ),
    )
    return configuration.model_dump_json()


def _response_body(provider: str) -> bytes:
    if provider == "openrouter":
        records = (
            {
                "response": {"status": "in_progress"},
                "type": "response.created",
            },
            {
                "delta": "ATLAS_SMOKE_OK",
                "type": "response.content_part.delta",
            },
            {
                "response": {
                    "openrouter_metadata": {"provider_name": "approved-endpoint"},
                    "status": "completed",
                    "usage": {"input_tokens": 8, "output_tokens": 4},
                },
                "type": "response.done",
            },
        )
    else:
        records = (
            {
                "response": {"status": "in_progress"},
                "sequence_number": 1,
                "type": "response.created",
            },
            {
                "delta": "ATLAS_SMOKE_OK",
                "sequence_number": 2,
                "type": "response.output_text.delta",
            },
            {
                "response": {
                    "status": "completed",
                    "usage": {"input_tokens": 8, "output_tokens": 4},
                },
                "sequence_number": 3,
                "type": "response.completed",
            },
        )
    lines = tuple(
        b"data: " + json.dumps(record, separators=(",", ":")).encode()
        for record in records
    )
    if provider == "openrouter":
        lines += (b"data: [DONE]",)
    return b"\n\n".join(lines) + b"\n\n"


def _write_private(path: Path, content: str) -> None:
    path.write_text(content)
    os.chmod(path, 0o600)
