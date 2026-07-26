"""Reusable offline configuration and streams for Bedrock smoke tests."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services.harness.protocol import (
    DataClassification,
    ProviderDataPolicy,
    ProviderModality,
    ProviderModelCapabilities,
    ProviderPriceRecord,
)
from app.services.harness.providers import (
    BedrockCredentialSourceKind,
    BedrockIdentityReference,
    BedrockRoutePolicy,
    ProviderConfiguration,
    ProviderCredentialBinding,
    ProviderRouteConfiguration,
    provider_destination_sha256,
)

NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)
ENDPOINT = "https://bedrock-runtime.us-east-1.amazonaws.com/"
MODEL_ID = "amazon.nova-lite-v1:0"
HANDLE = "pcr_" + "1" * 32
IDENTITY_ID = "awsid_" + "2" * 32


def configuration() -> ProviderConfiguration:
    model_revision = "3" * 64
    policy_revision = "4" * 64
    price_revision = "5" * 64
    destination_sha256 = provider_destination_sha256(ENDPOINT)
    model = ProviderModelCapabilities(
        provider="bedrock",
        model=MODEL_ID,
        model_revision_sha256=model_revision,
        catalog_snapshot_sha256="6" * 64,
        capabilities=(),
        input_modalities=(ProviderModality.TEXT,),
        output_modalities=(ProviderModality.TEXT,),
        context_features=(),
        available_regions=("us-east-1",),
        context_window_tokens=8_192,
        max_output_tokens=4_096,
        observed_at=NOW - timedelta(days=1),
    )
    policy = ProviderDataPolicy(
        provider="bedrock",
        policy_revision_sha256=policy_revision,
        destination_sha256=destination_sha256,
        accepted_classifications=(DataClassification.PUBLIC,),
        allowed_regions=("us-east-1",),
        retention_days=0,
        training_enabled=False,
        observed_at=NOW - timedelta(days=1),
    )
    price = ProviderPriceRecord(
        provider="bedrock",
        model=MODEL_ID,
        model_revision_sha256=model_revision,
        region="us-east-1",
        price_version_sha256=price_revision,
        input_microusd_per_million_tokens=1_000_000,
        cached_input_microusd_per_million_tokens=250_000,
        output_microusd_per_million_tokens=1_000_000,
        reasoning_microusd_per_million_tokens=1_000_000,
        effective_at=NOW - timedelta(days=1),
    )
    route = ProviderRouteConfiguration(
        route_id="bedrock.smoke",
        provider="bedrock",
        model=MODEL_ID,
        model_revision_sha256=model_revision,
        region="us-east-1",
        credential_handle=HANDLE,
        policy_revision_sha256=policy_revision,
        price_version_sha256=price_revision,
        priority=1,
        enabled=True,
    )
    return ProviderConfiguration(
        schema_version=1,
        models=(model,),
        credential_bindings=(
            ProviderCredentialBinding(
                handle=HANDLE,
                provider="bedrock",
                destination_sha256=destination_sha256,
            ),
        ),
        data_policies=(policy,),
        prices=(price,),
        routes=(route,),
    )


def route_policy() -> BedrockRoutePolicy:
    return BedrockRoutePolicy(
        route_id="bedrock.smoke",
        model_id=MODEL_ID,
        region="us-east-1",
        credential_handle=HANDLE,
        identity_reference_id=IDENTITY_ID,
        endpoint_url=ENDPOINT,
        destination_sha256=provider_destination_sha256(ENDPOINT),
        allowed_inference_regions=("us-east-1",),
    )


def identity(tmp_path: Path) -> BedrockIdentityReference:
    token_path = tmp_path / "web-identity-token"
    write_private(token_path, "disposable-account-token")
    return BedrockIdentityReference(
        identity_reference_id=IDENTITY_ID,
        credential_handle=HANDLE,
        source=BedrockCredentialSourceKind.WEB_IDENTITY,
        role_arn="arn:aws:iam::123456789012:role/atlas-bedrock-smoke",
        web_identity_token_file=token_path,
        role_session_name="atlas-smoke",
    )


def text_events() -> tuple[dict[str, object], ...]:
    return (
        {"messageStart": {"role": "assistant"}},
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"text": "ATLAS_SMOKE_OK"},
            }
        },
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {
            "metadata": {
                "metrics": {"latencyMs": 1},
                "usage": {
                    "inputTokens": 10,
                    "outputTokens": 4,
                    "totalTokens": 14,
                },
            }
        },
    )


def tool_events() -> tuple[dict[str, object], ...]:
    return (
        {"messageStart": {"role": "assistant"}},
        {
            "contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {
                    "toolUse": {
                        "name": "atlas_smoke_report",
                        "toolUseId": "tool-call-1",
                    }
                },
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {
                    "toolUse": {"input": '{"status":"ATLAS_SMOKE_OK"}'}
                },
            }
        },
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
        {
            "metadata": {
                "metrics": {"latencyMs": 1},
                "usage": {
                    "inputTokens": 12,
                    "outputTokens": 3,
                    "totalTokens": 15,
                },
            }
        },
    )


def write_private(path: Path, content: str) -> None:
    path.write_text(content)
    os.chmod(path, 0o600)
