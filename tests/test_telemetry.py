import io
import json
import logging
from uuid import UUID

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import CollectorRegistry

from app.core.config import TelemetrySettings
from app.core.telemetry import (
    JsonLogFormatter,
    PipelineOutcome,
    PipelineStage,
    QueueKind,
    Telemetry,
    TelemetryContext,
    log_event,
)
from app.main import app


def test_structured_logging_excludes_message_and_arbitrary_fields() -> None:
    stream = io.StringIO()
    logger = logging.getLogger("rag.test")
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    context = TelemetryContext(
        tenant_id=UUID("a7006ca9-bac4-4702-acaf-1e7c0dd6e7b6"),
        attempt=2,
    )

    log_event(logger, "pipeline.stage.completed", context)
    logger.info(
        "https://unsafe.example.test/source.pdf",
        extra={"document_text": "private source text"},
    )

    entries = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert entries[0]["event"] == "pipeline.stage.completed"
    assert entries[0]["tenant.id"] == str(context.tenant_id)
    assert entries[1]["event"] == "log"
    assert "unsafe.example" not in stream.getvalue()
    assert "private source text" not in stream.getvalue()


def test_metrics_have_bounded_labels_without_correlation_ids() -> None:
    telemetry = Telemetry(TelemetrySettings(), registry=CollectorRegistry())

    telemetry.record_queue(QueueKind.NATIVE, depth=3, oldest_age_seconds=12)
    telemetry.record_attempt(PipelineStage.NATIVE, PipelineOutcome.SUCCESS)
    telemetry.record_terminal_outcome(PipelineStage.NATIVE, PipelineOutcome.SUCCESS)
    telemetry.record_stage_duration(
        PipelineStage.NATIVE,
        PipelineOutcome.SUCCESS,
        duration_seconds=1.25,
    )
    metrics = telemetry.metrics().decode("utf-8")
    telemetry.close()

    assert 'rag_queue_depth{queue="native"} 3.0' in metrics
    assert 'rag_job_attempts_total{outcome="success",stage="native"} 1.0' in metrics
    assert "a7006ca9" not in metrics


def test_trace_span_contains_only_correlation_attributes() -> None:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(
        TelemetrySettings(),
        registry=CollectorRegistry(),
        span_exporter=exporter,
    )
    context = TelemetryContext(
        document_version_id=UUID("1dd21a4f-c0dc-45b4-8e36-5b9c9a7012a9"),
        pipeline_version="pypdf-v1",
    )

    with telemetry.span("ingestion.native", context):
        pass
    telemetry.close()

    span = exporter.get_finished_spans()[0]
    assert span.attributes["document.version_id"] == str(context.document_version_id)
    assert span.attributes["pipeline.version"] == "pypdf-v1"


def test_metrics_endpoint_exposes_prometheus_response() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/metrics")

    assert response.status_code == 200
    assert "rag_queue_depth" in response.text
