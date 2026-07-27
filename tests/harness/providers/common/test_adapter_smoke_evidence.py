import asyncio
import hashlib
from datetime import timedelta
from pathlib import Path

import pytest

from app.cli.harness.adapter_smoke_io import (
    build_adapter_smoke_result,
    write_adapter_smoke_result,
)
from app.cli.harness.provider_smoke_decode import EXPECTED_SMOKE_RESPONSE
from app.services.harness.protocol import (
    ProviderCompleted,
    ProviderFinishReason,
    ProviderReasoningDelta,
    ProviderTextDelta,
    ProviderTokenUsage,
    ProviderUsage,
)
from tests.harness_provider_capability_fixtures import NOW


def test_equivalent_events_produce_redacted_provider_neutral_shape() -> None:
    events = (
        ProviderTextDelta(sequence=1, text="ATLAS_"),
        ProviderTextDelta(sequence=2, text="SMOKE_OK"),
        ProviderUsage(
            sequence=3,
            usage=ProviderTokenUsage(
                input_tokens=3,
                cached_input_tokens=0,
                output_tokens=2,
                reasoning_tokens=0,
                cost_microusd=100,
            ),
        ),
        ProviderCompleted(
            sequence=4,
            finish_reason=ProviderFinishReason.STOP,
        ),
    )

    result = build_adapter_smoke_result(
        provider="vertex",
        model="configured-model",
        request_id="req_" + "1" * 32,
        events=events,
        route_binding_sha256="a" * 64,
        latency_ms=10,
        cancellation_latency_ms=2,
        charged_cost_microusd=100,
        completed_at=NOW,
    )

    assert result.canonical_shape == "text_usage_completed"
    assert result.output_sha256 == hashlib.sha256(
        EXPECTED_SMOKE_RESPONSE.encode()
    ).hexdigest()
    assert result.event_count == 4
    assert EXPECTED_SMOKE_RESPONSE not in result.model_dump_json()


@pytest.mark.parametrize(
    "events",
    (
        (
            ProviderTextDelta(sequence=1, text="wrong"),
            ProviderUsage(
                sequence=2,
                usage=ProviderTokenUsage(
                    input_tokens=1,
                    cached_input_tokens=0,
                    output_tokens=1,
                    reasoning_tokens=0,
                    cost_microusd=0,
                ),
            ),
            ProviderCompleted(
                sequence=3,
                finish_reason=ProviderFinishReason.STOP,
            ),
        ),
        (
            ProviderTextDelta(sequence=1, text=EXPECTED_SMOKE_RESPONSE),
            ProviderReasoningDelta(sequence=2, text="unexpected"),
            ProviderUsage(
                sequence=3,
                usage=ProviderTokenUsage(
                    input_tokens=1,
                    cached_input_tokens=0,
                    output_tokens=1,
                    reasoning_tokens=1,
                    cost_microusd=0,
                ),
            ),
            ProviderCompleted(
                sequence=4,
                finish_reason=ProviderFinishReason.STOP,
            ),
        ),
    ),
)
def test_wrong_output_or_non_equivalent_shape_is_rejected(events) -> None:
    with pytest.raises(ValueError, match="adapter smoke"):
        build_adapter_smoke_result(
            provider="local-compatible",
            model="configured-model",
            request_id="req_" + "1" * 32,
            events=events,
            route_binding_sha256="a" * 64,
            latency_ms=10,
            cancellation_latency_ms=2,
            charged_cost_microusd=0,
            completed_at=NOW,
        )


def test_result_file_contains_hashes_not_provider_output(
    tmp_path: Path,
) -> None:
    result = build_adapter_smoke_result(
        provider="local-compatible",
        model="configured-model",
        request_id="req_" + "1" * 32,
        events=_events(),
        route_binding_sha256="b" * 64,
        latency_ms=10,
        cancellation_latency_ms=2,
        charged_cost_microusd=0,
        completed_at=NOW + timedelta(seconds=1),
    )
    os_mode = tmp_path.stat().st_mode
    tmp_path.chmod(os_mode & ~0o077)
    result_path = tmp_path / "result.json"

    asyncio.run(write_adapter_smoke_result(result_path, result))

    persisted = result_path.read_text()
    assert EXPECTED_SMOKE_RESPONSE not in persisted
    assert result.output_sha256 in persisted


def _events():
    return (
        ProviderTextDelta(sequence=1, text=EXPECTED_SMOKE_RESPONSE),
        ProviderUsage(
            sequence=2,
            usage=ProviderTokenUsage(
                input_tokens=1,
                cached_input_tokens=0,
                output_tokens=1,
                reasoning_tokens=0,
                cost_microusd=0,
            ),
        ),
        ProviderCompleted(
            sequence=3,
            finish_reason=ProviderFinishReason.STOP,
        ),
    )
