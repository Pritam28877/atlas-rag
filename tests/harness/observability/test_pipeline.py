"""Redaction, metrics, and bounded exporter tests."""

import asyncio

import pytest

from app.services.harness.observability import (
    BoundedMetricRegistry,
    BoundedTelemetryPipeline,
    TelemetryMetric,
    redact,
)


def test_nested_canaries_are_redacted_and_bounded() -> None:
    result = redact(
        {
            "prompt": "private prompt",
            "nested": {"Authorization": "Bearer secret", "safe": "ok"},
            "items": list(range(100)),
        }
    )
    assert result.value["prompt"] == "<redacted>"
    assert result.value["nested"]["Authorization"] == "<redacted>"
    assert result.truncated
    assert "private prompt" not in str(result.value)


def test_metrics_use_approved_names_and_fixed_cardinality() -> None:
    registry = BoundedMetricRegistry(("harness.turns",), maximum_metrics=1)
    registry.record(
        TelemetryMetric(
            name="harness.turns",
            labels=(),
            value=1,
        )
    )
    with pytest.raises(ValueError, match="approved catalog"):
        registry.record(TelemetryMetric(name="user.email", labels=(), value=1))
    assert registry.snapshot()[0].value == 1


def test_disabled_pipeline_does_not_retain_sensitive_data() -> None:
    pipeline = BoundedTelemetryPipeline(enabled=False)
    assert not pipeline.emit("harness.turn", {"prompt": "secret"})
    assert pipeline.queue_size() == 0


def test_export_failure_is_bounded_and_dropped() -> None:
    pipeline = BoundedTelemetryPipeline(
        enabled=True,
        maximum_queue=1,
        maximum_retries=1,
    )
    assert pipeline.emit("harness.turn", {"safe": "value"})
    assert not pipeline.emit("harness.turn", {"safe": "second"})

    async def failing_export(_batch: tuple[object, ...]) -> None:
        raise RuntimeError("export unavailable")

    result = asyncio.run(pipeline.flush(failing_export))
    assert result.exported == 0
    assert result.dropped == 2
    assert result.retries == 1
