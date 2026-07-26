import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from app.services.harness.protocol import (
    BlobPayload,
    CanonicalProviderRequest,
    DataClassification,
    InlinePayload,
    ProviderContentPart,
    ProviderContextFeature,
    ProviderCredentialHandle,
    ProviderDataPolicy,
    ProviderFailureClass,
    ProviderMessage,
    ProviderMessageRole,
    ProviderModality,
    ProviderModelCapabilities,
    ProviderPriceRecord,
    ProviderRetryDisposition,
    ProviderTokenUsage,
    ProviderToolDefinition,
    ProviderTransportFailure,
    ProviderUsageMetadata,
    price_is_active,
)

NOW = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)
DIGEST = "0" * 64
SCHEMA = '{"additionalProperties":false,"type":"object"}'


def inline_payload(text: str = "provider-neutral input") -> InlinePayload:
    encoded = text.encode()
    return InlinePayload(
        text=text,
        size_bytes=len(encoded),
        content_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def tool(name: str = "search") -> ProviderToolDefinition:
    return ProviderToolDefinition(
        name=name,
        version="1.0.0",
        description="Search approved evidence.",
        input_schema_json=SCHEMA,
        input_schema_sha256=hashlib.sha256(SCHEMA.encode()).hexdigest(),
    )


def request(**overrides: object) -> CanonicalProviderRequest:
    values: dict[str, object] = {
        "request_id": "req_" + "1" * 32,
        "turn_id": "trn_" + "2" * 32,
        "route_id": "primary.route",
        "classification": DataClassification.CONFIDENTIAL,
        "messages": (
            ProviderMessage(
                role=ProviderMessageRole.USER,
                parts=(
                    ProviderContentPart(
                        modality=ProviderModality.TEXT,
                        payload=inline_payload(),
                    ),
                ),
            ),
        ),
        "tools": (tool(),),
        "required_capabilities": ("reasoning", "tools"),
        "output_modalities": (ProviderModality.TEXT,),
        "reserved_output_tokens": 4_096,
        "reserved_reasoning_tokens": 1_024,
        "deadline_at": NOW + timedelta(seconds=30),
    }
    values.update(overrides)
    return CanonicalProviderRequest.model_validate(values)


def model_capabilities(
    **overrides: object,
) -> ProviderModelCapabilities:
    values: dict[str, object] = {
        "provider": "configured-provider",
        "model": "configured-model",
        "model_revision_sha256": "1" * 64,
        "catalog_snapshot_sha256": "2" * 64,
        "capabilities": ("reasoning", "tools"),
        "input_modalities": (
            ProviderModality.AUDIO,
            ProviderModality.TEXT,
        ),
        "output_modalities": (ProviderModality.TEXT,),
        "context_features": (
            ProviderContextFeature.NATIVE_COMPACTION,
            ProviderContextFeature.PROMPT_CACHE,
        ),
        "available_regions": ("ap-south-1", "us-east-1"),
        "context_window_tokens": 128_000,
        "max_output_tokens": 16_000,
        "observed_at": NOW,
    }
    values.update(overrides)
    return ProviderModelCapabilities.model_validate(values)


def price(**overrides: object) -> ProviderPriceRecord:
    values: dict[str, object] = {
        "provider": "configured-provider",
        "model": "configured-model",
        "model_revision_sha256": "1" * 64,
        "region": "us-east-1",
        "price_version_sha256": "3" * 64,
        "input_microusd_per_million_tokens": 2_000_000,
        "cached_input_microusd_per_million_tokens": 500_000,
        "output_microusd_per_million_tokens": 8_000_000,
        "reasoning_microusd_per_million_tokens": 8_000_000,
        "effective_at": NOW,
        "expires_at": NOW + timedelta(days=30),
    }
    values.update(overrides)
    return ProviderPriceRecord.model_validate(values)


def test_canonical_request_is_bounded_ordered_and_provider_neutral() -> None:
    canonical = request()

    assert canonical.route_id == "primary.route"
    assert canonical.messages[0].parts[0].payload == inline_payload()
    assert "openai" not in canonical.model_dump_json()
    with pytest.raises(ValidationError, match="unique and sorted"):
        request(required_capabilities=("tools", "reasoning"))
    with pytest.raises(ValidationError, match="unique and sorted"):
        request(tools=(tool("write"), tool("search")))
    with pytest.raises(ValidationError, match="tool call ID"):
        ProviderMessage(
            role=ProviderMessageRole.TOOL,
            parts=(
                ProviderContentPart(
                    modality=ProviderModality.TEXT,
                    payload=inline_payload("tool result"),
                ),
            ),
        )


def test_tool_schema_and_total_request_bytes_fail_closed() -> None:
    with pytest.raises(ValidationError, match="canonical JSON"):
        ProviderToolDefinition(
            name="search",
            version="1.0.0",
            description="Search.",
            input_schema_json='{"type": "object"}',
            input_schema_sha256=hashlib.sha256(
                b'{"type": "object"}'
            ).hexdigest(),
        )
    oversized_payload = BlobPayload(
        artifact_id="art_" + "4" * 32,
        media_type="application/octet-stream",
        size_bytes=16 * 1024 * 1024,
        content_sha256="4" * 64,
    )
    with pytest.raises(ValidationError, match="exceeds 16 MiB"):
        request(
            messages=(
                ProviderMessage(
                    role=ProviderMessageRole.USER,
                    parts=(
                        ProviderContentPart(
                            modality=ProviderModality.DOCUMENT,
                            payload=oversized_payload,
                        ),
                    ),
                ),
            )
        )


def test_model_and_data_policy_records_are_canonical_and_secret_free() -> None:
    capabilities = model_capabilities()
    policy = ProviderDataPolicy(
        provider=capabilities.provider,
        policy_revision_sha256="5" * 64,
        destination_sha256="6" * 64,
        accepted_classifications=(
            DataClassification.CONFIDENTIAL,
            DataClassification.INTERNAL,
        ),
        allowed_regions=("ap-south-1", "us-east-1"),
        retention_days=30,
        training_enabled=False,
        observed_at=NOW,
    )
    handle_adapter = TypeAdapter(ProviderCredentialHandle)

    assert policy.destination_sha256 == "6" * 64
    assert handle_adapter.validate_python("pcr_" + "7" * 32, strict=True)
    with pytest.raises(ValidationError):
        handle_adapter.validate_python("raw-provider-api-key", strict=True)
    with pytest.raises(ValidationError, match="unique and sorted"):
        model_capabilities(available_regions=("us-east-1", "ap-south-1"))
    with pytest.raises(ValidationError, match="exceeds its context window"):
        model_capabilities(
            context_window_tokens=8_000,
            max_output_tokens=8_001,
        )


def test_prices_and_usage_are_revision_pinned() -> None:
    current_price = price()
    usage = ProviderUsageMetadata(
        route_id="primary.route",
        provider_request_sha256="8" * 64,
        model_revision_sha256=current_price.model_revision_sha256,
        price_version_sha256=current_price.price_version_sha256,
        usage=ProviderTokenUsage(
            input_tokens=1_000,
            cached_input_tokens=250,
            output_tokens=200,
            reasoning_tokens=50,
            cost_microusd=3_725,
        ),
        estimated_cost_microusd=4_000,
        recorded_at=NOW + timedelta(seconds=1),
    )

    assert usage.price_version_sha256 == current_price.price_version_sha256
    assert price_is_active(current_price, NOW)
    assert not price_is_active(current_price, NOW + timedelta(days=30))
    with pytest.raises(ValueError, match="must use UTC"):
        price_is_active(current_price, datetime(2026, 7, 28, 9, 0))
    with pytest.raises(ValidationError, match="must follow"):
        price(expires_at=NOW)


def test_transport_failure_retry_evidence_is_unambiguous() -> None:
    failure_values: dict[str, object] = {
        "failure_class": ProviderFailureClass.RATE_LIMIT,
        "retry_disposition": ProviderRetryDisposition.ELIGIBLE,
        "code": "rate_limit",
        "reason": "Provider requested bounded backoff.",
        "provider_request_sha256": DIGEST,
        "http_status": 429,
        "retry_after_ms": 1_000,
        "ambiguous": False,
        "occurred_at": NOW,
    }
    failure = ProviderTransportFailure.model_validate(failure_values)

    assert failure.retry_after_ms == 1_000
    with pytest.raises(ValidationError, match="not eligible"):
        ProviderTransportFailure.model_validate(
            {
                **failure_values,
                "failure_class": ProviderFailureClass.AUTHENTICATION,
            }
        )
    with pytest.raises(ValidationError, match="ambiguous"):
        ProviderTransportFailure.model_validate(
            {**failure_values, "ambiguous": True}
        )
    with pytest.raises(ValidationError, match="retry hint"):
        ProviderTransportFailure.model_validate(
            {
                **failure_values,
                "retry_disposition": ProviderRetryDisposition.PROHIBITED,
            }
        )
