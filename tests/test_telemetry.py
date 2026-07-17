import asyncio
import io
import json
import logging
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import CollectorRegistry

from app.api.v1.metrics import router as metrics_router
from app.core.config import Settings, TelemetrySettings
from app.core.telemetry import (
    JsonLogFormatter,
    MetricsUnavailableError,
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
    telemetry.record_queue(QueueKind.LIFECYCLE, depth=1, oldest_age_seconds=4)
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
    assert 'rag_queue_depth{queue="lifecycle"} 1.0' in metrics
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


def test_database_metrics_refresh_is_coalesced_and_cached() -> None:
    telemetry = Telemetry(
        TelemetrySettings(metrics_cache_ttl_seconds=10),
        registry=CollectorRegistry(),
    )
    telemetry._read_database_metrics = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            [{"queue": "publication", "depth": 2, "oldest_age": 7.5}],
            12.0,
        )
    )

    async def refresh_concurrently() -> None:
        await asyncio.gather(
            telemetry.refresh_from_database(object()),  # type: ignore[arg-type]
            telemetry.refresh_from_database(object()),  # type: ignore[arg-type]
            telemetry.refresh_from_database(object()),  # type: ignore[arg-type]
        )

    asyncio.run(refresh_concurrently())
    metrics = telemetry.metrics().decode("utf-8")
    assert telemetry._read_database_metrics.await_count == 1
    assert 'rag_queue_depth{queue="publication"} 2.0' in metrics
    assert "rag_scheduler_heartbeat_present 1.0" in metrics
    telemetry.close()


def test_database_metrics_failure_requires_an_initial_snapshot() -> None:
    telemetry = Telemetry(TelemetrySettings(), registry=CollectorRegistry())
    telemetry._read_database_metrics = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("database unavailable")
    )

    with pytest.raises(MetricsUnavailableError, match="snapshot is unavailable"):
        asyncio.run(
            telemetry.refresh_from_database(object())  # type: ignore[arg-type]
        )

    metrics = telemetry.metrics().decode("utf-8")
    assert "rag_queue_metrics_refresh_success 0.0" in metrics
    assert "database unavailable" not in metrics
    telemetry.close()


def test_database_metrics_failure_retains_last_snapshot() -> None:
    telemetry = Telemetry(TelemetrySettings(), registry=CollectorRegistry())
    telemetry._read_database_metrics = AsyncMock(  # type: ignore[method-assign]
        return_value=([{"queue": "native", "depth": 4, "oldest_age": 3}], None)
    )
    asyncio.run(telemetry.refresh_from_database(object()))  # type: ignore[arg-type]
    telemetry._metrics_refresh_after = 0
    telemetry._read_database_metrics.side_effect = RuntimeError("database unavailable")

    asyncio.run(telemetry.refresh_from_database(object()))  # type: ignore[arg-type]

    metrics = telemetry.metrics().decode("utf-8")
    assert 'rag_queue_depth{queue="native"} 4.0' in metrics
    assert "rag_queue_metrics_refresh_success 0.0" in metrics
    telemetry.close()


def test_metrics_endpoint_authenticates_before_database_refresh() -> None:
    settings = Settings(
        _env_file=None,
        telemetry=TelemetrySettings(metrics_bearer_token="metrics-test-token"),
    )
    telemetry = Telemetry(settings.telemetry, registry=CollectorRegistry())
    telemetry._read_database_metrics = AsyncMock(  # type: ignore[method-assign]
        return_value=([], None)
    )
    metrics_app = FastAPI()
    metrics_app.state.settings = settings
    metrics_app.state.telemetry = telemetry
    metrics_app.state.database = object()
    metrics_app.include_router(metrics_router, prefix="/v1")

    with TestClient(metrics_app) as client:
        unauthorized = client.get("/v1/metrics")
        authorized = client.get(
            "/v1/metrics",
            headers={"Authorization": "Bearer metrics-test-token"},
        )

    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert authorized.status_code == 200
    assert telemetry._read_database_metrics.await_count == 1
    telemetry.close()


def test_metrics_endpoint_returns_503_without_initial_database_snapshot() -> None:
    settings = Settings(_env_file=None)
    telemetry = Telemetry(settings.telemetry, registry=CollectorRegistry())
    telemetry._read_database_metrics = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("database unavailable")
    )
    metrics_app = FastAPI()
    metrics_app.state.settings = settings
    metrics_app.state.telemetry = telemetry
    metrics_app.state.database = object()
    metrics_app.include_router(metrics_router, prefix="/v1")

    with TestClient(metrics_app) as client:
        response = client.get("/v1/metrics")

    assert response.status_code == 503
    assert response.json() == {"detail": "metrics are temporarily unavailable"}
    telemetry.close()
