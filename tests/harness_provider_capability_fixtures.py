"""Provider-neutral fixtures shared by capability trait tests."""

import hashlib
from datetime import UTC, datetime, timedelta

from app.services.harness.protocol import (
    CanonicalProviderRequest,
    DataClassification,
    InlinePayload,
    ProviderContentPart,
    ProviderContextFeature,
    ProviderDataPolicy,
    ProviderMessage,
    ProviderMessageRole,
    ProviderModality,
    ProviderModelCapabilities,
    ProviderPriceRecord,
)

NOW = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)


def request() -> CanonicalProviderRequest:
    text = "independent provider capabilities"
    encoded = text.encode()
    payload = InlinePayload(
        text=text,
        size_bytes=len(encoded),
        content_sha256=hashlib.sha256(encoded).hexdigest(),
    )
    return CanonicalProviderRequest(
        request_id="req_" + "1" * 32,
        turn_id="trn_" + "2" * 32,
        route_id="configured.primary",
        classification=DataClassification.CONFIDENTIAL,
        messages=(
            ProviderMessage(
                role=ProviderMessageRole.USER,
                parts=(
                    ProviderContentPart(
                        modality=ProviderModality.TEXT,
                        payload=payload,
                    ),
                ),
            ),
        ),
        tools=(),
        required_capabilities=(),
        output_modalities=(ProviderModality.TEXT,),
        reserved_output_tokens=1_024,
        reserved_reasoning_tokens=0,
        deadline_at=NOW + timedelta(seconds=30),
    )


def model() -> ProviderModelCapabilities:
    return ProviderModelCapabilities(
        provider="configured-provider",
        model="configured-model",
        model_revision_sha256="3" * 64,
        catalog_snapshot_sha256="4" * 64,
        capabilities=(),
        input_modalities=(ProviderModality.TEXT,),
        output_modalities=(ProviderModality.TEXT,),
        context_features=(ProviderContextFeature.PROMPT_CACHE,),
        available_regions=("us-east-1",),
        context_window_tokens=32_000,
        max_output_tokens=4_096,
        observed_at=NOW,
    )


def policy() -> ProviderDataPolicy:
    return ProviderDataPolicy(
        provider="configured-provider",
        policy_revision_sha256="5" * 64,
        destination_sha256="6" * 64,
        accepted_classifications=(DataClassification.CONFIDENTIAL,),
        allowed_regions=("us-east-1",),
        retention_days=0,
        training_enabled=False,
        observed_at=NOW,
    )


def price() -> ProviderPriceRecord:
    return ProviderPriceRecord(
        provider="configured-provider",
        model="configured-model",
        model_revision_sha256="3" * 64,
        region="us-east-1",
        price_version_sha256="7" * 64,
        input_microusd_per_million_tokens=1_000_000,
        cached_input_microusd_per_million_tokens=250_000,
        output_microusd_per_million_tokens=2_000_000,
        reasoning_microusd_per_million_tokens=2_000_000,
        effective_at=NOW,
    )
