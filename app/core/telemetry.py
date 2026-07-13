"""Redacted structured logs, bounded metrics, and trace correlation."""

import json
import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import SpanKind
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from app.core.config import ProviderTimeoutSettings, TelemetrySettings

SAFE_EVENT = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")


class PipelineStage(StrEnum):
    INTAKE = "intake"
    NATIVE = "native"
    OCR = "ocr"
    CHUNKING = "chunking"
    INDEXING = "indexing"
    DELETION = "deletion"


class PipelineOutcome(StrEnum):
    SUCCESS = "success"
    RETRY = "retry"
    FAILED = "failed"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    CANCELLED = "cancelled"


class QueueKind(StrEnum):
    NATIVE = "native"
    OCR = "ocr"
    DEAD_LETTER = "dead-letter"


@dataclass(frozen=True)
class TelemetryContext:
    """Correlation identifiers that are safe only in logs and traces."""

    tenant_id: UUID | None = None
    collection_id: UUID | None = None
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    job_id: UUID | None = None
    attempt: int | None = None
    pipeline_version: str | None = None

    def attributes(self) -> dict[str, str | int]:
        values = {
            "tenant.id": self.tenant_id,
            "collection.id": self.collection_id,
            "document.id": self.document_id,
            "document.version_id": self.document_version_id,
            "job.id": self.job_id,
            "job.attempt": self.attempt,
            "pipeline.version": self.pipeline_version,
        }
        return {
            key: str(value) if isinstance(value, UUID) else value
            for key, value in values.items()
            if value is not None
        }


class JsonLogFormatter(logging.Formatter):
    """Emit only whitelisted structured fields, ignoring arbitrary log data."""

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", "log")
        if not isinstance(event, str) or not SAFE_EVENT.fullmatch(event):
            event = "invalid-log-event"
        correlation = getattr(record, "correlation", TelemetryContext())
        context = (
            correlation.attributes()
            if isinstance(correlation, TelemetryContext)
            else {}
        )
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": event,
            **context,
        }
        error_code = getattr(record, "error_code", None)
        if isinstance(error_code, str) and SAFE_EVENT.fullmatch(error_code):
            payload["error.code"] = error_code
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def configure_structured_logging(settings: TelemetrySettings) -> logging.Logger:
    """Configure the dedicated application logger without mutating root logging."""
    logger = logging.getLogger("rag")
    logger.setLevel(settings.log_level)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def log_event(
    logger: logging.Logger,
    event: str,
    correlation: TelemetryContext = TelemetryContext(),
    error_code: str | None = None,
) -> None:
    """Log a safe event without accepting source content or arbitrary context."""
    logger.info(
        event,
        extra={"event": event, "correlation": correlation, "error_code": error_code},
    )


class Telemetry:
    """Own application metrics and trace export resources."""

    def __init__(
        self,
        settings: TelemetrySettings,
        registry: CollectorRegistry | None = None,
        span_exporter: SpanExporter | None = None,
        provider_timeouts: ProviderTimeoutSettings | None = None,
    ) -> None:
        self._metrics_enabled = settings.metrics_enabled
        self._registry = registry or CollectorRegistry()
        self.queue_depth = Gauge(
            "rag_queue_depth",
            "Current number of queued ingestion jobs.",
            ["queue"],
            registry=self._registry,
        )
        self.queue_age_seconds = Gauge(
            "rag_queue_oldest_age_seconds",
            "Age of the oldest queued ingestion job.",
            ["queue"],
            registry=self._registry,
        )
        self.job_attempts = Counter(
            "rag_job_attempts_total",
            "Ingestion job attempts by stage and outcome.",
            ["stage", "outcome"],
            registry=self._registry,
        )
        self.terminal_outcomes = Counter(
            "rag_job_terminal_outcomes_total",
            "Terminal ingestion outcomes by stage and outcome.",
            ["stage", "outcome"],
            registry=self._registry,
        )
        self.stage_duration_seconds = Histogram(
            "rag_stage_duration_seconds",
            "Ingestion stage duration in seconds.",
            ["stage", "outcome"],
            registry=self._registry,
        )
        effective_timeouts = provider_timeouts or ProviderTimeoutSettings()
        provider = TracerProvider(
            resource=Resource.create({SERVICE_NAME: settings.service_name})
        )
        if span_exporter is not None:
            provider.add_span_processor(BatchSpanProcessor(span_exporter))
        elif settings.traces_enabled and settings.otlp_endpoint:
            provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=settings.otlp_endpoint,
                        timeout=effective_timeouts.request_seconds,
                    )
                )
            )
        self._provider = provider
        self._tracer = provider.get_tracer(settings.service_name)

    def record_queue(
        self, queue: QueueKind, depth: int, oldest_age_seconds: float
    ) -> None:
        """Record bounded queue backlog state without tenant-specific labels."""
        if not self._metrics_enabled:
            return
        self.queue_depth.labels(queue).set(depth)
        self.queue_age_seconds.labels(queue).set(oldest_age_seconds)

    def record_attempt(self, stage: PipelineStage, outcome: PipelineOutcome) -> None:
        """Record one classified attempt."""
        if self._metrics_enabled:
            self.job_attempts.labels(stage, outcome).inc()

    def record_terminal_outcome(
        self,
        stage: PipelineStage,
        outcome: PipelineOutcome,
    ) -> None:
        """Record one terminal classified result."""
        if self._metrics_enabled:
            self.terminal_outcomes.labels(stage, outcome).inc()

    def record_stage_duration(
        self,
        stage: PipelineStage,
        outcome: PipelineOutcome,
        duration_seconds: float,
    ) -> None:
        """Record duration after a completed stage attempt."""
        if self._metrics_enabled:
            self.stage_duration_seconds.labels(stage, outcome).observe(duration_seconds)

    @contextmanager
    def span(self, name: str, correlation: TelemetryContext) -> Iterator[None]:
        """Create one trace span with correlation identifiers but no source content."""
        with self._tracer.start_as_current_span(name, kind=SpanKind.INTERNAL) as span:
            span.set_attributes(correlation.attributes())
            yield

    def metrics(self) -> bytes:
        """Render Prometheus text without tenant/document labels."""
        return generate_latest(self._registry)

    def close(self) -> None:
        """Flush and stop trace exporter resources during application shutdown."""
        self._provider.shutdown()
