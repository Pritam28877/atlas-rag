"""Bounded redacted telemetry events, metrics, and export backpressure."""

from __future__ import annotations

import asyncio
import hashlib
import math
import secrets
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.observability.redaction import (
    MAXIMUM_REDACTION_ENTRIES,
    RedactionResult,
    canonical_digest,
    redact,
)
from app.services.harness.protocol import Sha256, StrictProtocolModel

MAXIMUM_TELEMETRY_QUEUE = 4_096
MAXIMUM_TELEMETRY_BATCH = 256
MAXIMUM_TELEMETRY_METRICS = 256
MAXIMUM_METRIC_LABELS = 8
MAXIMUM_METRIC_LABEL_VALUE = 64


class TelemetryField(StrictProtocolModel):
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=MAXIMUM_METRIC_LABEL_VALUE)


class TelemetryEvent(StrictProtocolModel):
    event_name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")
    trace_id: str = Field(min_length=4, max_length=68, pattern=r"^trc_[0-9a-f]{64}$")
    fields: tuple[TelemetryField, ...] = Field(max_length=MAXIMUM_REDACTION_ENTRIES)
    redacted_fields: tuple[str, ...] = Field(max_length=MAXIMUM_REDACTION_ENTRIES)
    truncated: bool
    event_sha256: Sha256

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        keys = tuple(field.key for field in self.fields)
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("telemetry field keys must be unique and sorted")
        return self


class TelemetryMetric(StrictProtocolModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")
    labels: tuple[TelemetryField, ...] = Field(max_length=MAXIMUM_METRIC_LABELS)
    value: float

    @model_validator(mode="after")
    def validate_metric(self) -> Self:
        if not math.isfinite(self.value):
            raise ValueError("metric value must be finite")
        keys = tuple(label.key for label in self.labels)
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("metric label keys must be unique and sorted")
        return self


class TelemetryFlushResult(StrictProtocolModel):
    enabled: bool
    exported: int = Field(ge=0, le=MAXIMUM_TELEMETRY_BATCH)
    dropped: int = Field(ge=0)
    retries: int = Field(ge=0, le=3)


TelemetryExporter = Callable[[tuple[TelemetryEvent, ...]], Awaitable[None]]


class BoundedTelemetryPipeline:
    """Redacts synchronously, then exports from a bounded queue."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        maximum_queue: int = MAXIMUM_TELEMETRY_QUEUE,
        maximum_batch: int = MAXIMUM_TELEMETRY_BATCH,
        maximum_retries: int = 2,
    ) -> None:
        if not enabled and maximum_queue < 1:
            raise ValueError("disabled telemetry still requires valid bounds")
        if not 1 <= maximum_queue <= MAXIMUM_TELEMETRY_QUEUE:
            raise ValueError("telemetry queue bound is outside the limit")
        if not 1 <= maximum_batch <= MAXIMUM_TELEMETRY_BATCH:
            raise ValueError("telemetry batch bound is outside the limit")
        if not 0 <= maximum_retries <= 3:
            raise ValueError("telemetry retry bound is outside the limit")
        self._enabled = enabled
        self._maximum_queue = maximum_queue
        self._maximum_batch = maximum_batch
        self._maximum_retries = maximum_retries
        self._events: deque[TelemetryEvent] = deque()
        self._dropped = 0

    def emit(
        self,
        event_name: str,
        attributes: Mapping[str, object],
        *,
        trace_id: str | None = None,
    ) -> bool:
        if not self._enabled:
            return False
        redaction = redact(attributes)
        fields = _fields_from_redaction(redaction)
        resolved_trace_id = trace_id or _new_trace_id()
        if not resolved_trace_id.startswith("trc_"):
            resolved_trace_id = _new_trace_id()
        event_hash = hashlib.sha256(
            f"{event_name}:{canonical_digest(redaction.value)}".encode()
        ).hexdigest()
        event = TelemetryEvent(
            event_name=event_name,
            trace_id=resolved_trace_id,
            fields=fields,
            redacted_fields=redaction.redacted_fields,
            truncated=redaction.truncated,
            event_sha256=event_hash,
        )
        if len(self._events) >= self._maximum_queue:
            self._dropped += 1
            return False
        self._events.append(event)
        return True

    def queue_size(self) -> int:
        return len(self._events)

    async def flush(self, exporter: TelemetryExporter | None) -> TelemetryFlushResult:
        if not self._enabled:
            return TelemetryFlushResult(enabled=False, exported=0, dropped=0, retries=0)
        if exporter is None or not self._events:
            return TelemetryFlushResult(
                enabled=True,
                exported=0,
                dropped=self._dropped,
                retries=0,
            )
        batch = tuple(
            self._events.popleft()
            for _ in range(min(self._maximum_batch, len(self._events)))
        )
        retries = 0
        while True:
            try:
                async with asyncio.timeout(5):
                    await exporter(batch)
                return TelemetryFlushResult(
                    enabled=True,
                    exported=len(batch),
                    dropped=self._dropped,
                    retries=retries,
                )
            except Exception:
                if retries >= self._maximum_retries:
                    self._dropped += len(batch)
                    return TelemetryFlushResult(
                        enabled=True,
                        exported=0,
                        dropped=self._dropped,
                        retries=retries,
                    )
                retries += 1


class BoundedMetricRegistry:
    """Fixed-cardinality metric store with no user identifiers in labels."""

    def __init__(
        self,
        metric_names: tuple[str, ...],
        *,
        maximum_metrics: int = MAXIMUM_TELEMETRY_METRICS,
    ) -> None:
        if not 1 <= len(metric_names) <= MAXIMUM_TELEMETRY_METRICS:
            raise ValueError("metric catalog is outside the bounded limit")
        if tuple(sorted(set(metric_names))) != metric_names:
            raise ValueError("metric names must be unique and sorted")
        if not 1 <= maximum_metrics <= MAXIMUM_TELEMETRY_METRICS:
            raise ValueError("metric capacity is outside the bounded limit")
        self._metric_names = frozenset(metric_names)
        self._maximum_metrics = maximum_metrics
        self._values: dict[tuple[str, tuple[TelemetryField, ...]], float] = {}

    def record(self, metric: TelemetryMetric) -> None:
        if metric.name not in self._metric_names:
            raise ValueError("metric is not in the approved catalog")
        if len(self._values) >= self._maximum_metrics and (
            metric.name,
            metric.labels,
        ) not in self._values:
            raise ValueError("metric cardinality capacity is exhausted")
        key = (metric.name, metric.labels)
        self._values[key] = self._values.get(key, 0.0) + metric.value

    def snapshot(self) -> tuple[TelemetryMetric, ...]:
        return tuple(
            TelemetryMetric(name=name, labels=labels, value=value)
            for (name, labels), value in sorted(self._values.items())
        )


def _fields_from_redaction(result: RedactionResult) -> tuple[TelemetryField, ...]:
    if not isinstance(result.value, Mapping):
        return ()
    values = []
    for key, value in result.value.items():
        if isinstance(value, (str, int, float, bool)):
            values.append(
                TelemetryField(
                    key=str(key)[:64],
                    value=str(value)[:MAXIMUM_METRIC_LABEL_VALUE],
                )
            )
    return tuple(sorted(values, key=lambda field: field.key))


def _new_trace_id() -> str:
    return "trc_" + hashlib.sha256(secrets.token_bytes(32)).hexdigest()
