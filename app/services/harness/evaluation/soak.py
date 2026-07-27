"""Bounded resource sampling for long-running harness soak evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import Field

from app.services.harness.protocol.base import StrictProtocolModel, UtcTimestamp

MAXIMUM_SOAK_SAMPLES = 100_000


class SoakBudget(StrictProtocolModel):
    duration_seconds: int = Field(ge=1, le=7 * 24 * 60 * 60)
    sample_interval_seconds: int = Field(ge=1, le=3_600)
    maximum_rss_bytes: int = Field(ge=1)
    maximum_queue_depth: int = Field(ge=1)
    maximum_storage_bytes: int = Field(ge=1)
    maximum_retries: int = Field(ge=0)
    maximum_processes: int = Field(ge=1)
    maximum_subscribers: int = Field(ge=1)
    maximum_cost_microusd: int = Field(ge=0)


class SoakSample(StrictProtocolModel):
    observed_at: UtcTimestamp
    elapsed_seconds: int = Field(ge=0)
    rss_bytes: int = Field(ge=0)
    queue_depth: int = Field(ge=0)
    storage_bytes: int = Field(ge=0)
    retries: int = Field(ge=0)
    process_count: int = Field(ge=0)
    subscribers: int = Field(ge=0)
    cost_microusd: int = Field(ge=0)


class SoakSummary(StrictProtocolModel):
    sample_count: int = Field(ge=0, le=MAXIMUM_SOAK_SAMPLES)
    elapsed_seconds: int = Field(ge=0)
    maximum_rss_bytes: int = Field(ge=0)
    maximum_queue_depth: int = Field(ge=0)
    maximum_storage_bytes: int = Field(ge=0)
    maximum_retries: int = Field(ge=0)
    maximum_processes: int = Field(ge=0)
    maximum_subscribers: int = Field(ge=0)
    maximum_cost_microusd: int = Field(ge=0)
    violations: tuple[str, ...] = Field(max_length=16)
    complete: bool


@dataclass
class SoakAccumulator:
    """O(1)-memory accumulator; raw samples stay in the evidence store."""

    budget: SoakBudget
    _sample_count: int = 0
    _last_observed_at: datetime | None = None
    _elapsed_seconds: int = 0
    _maximum_rss_bytes: int = 0
    _maximum_queue_depth: int = 0
    _maximum_storage_bytes: int = 0
    _maximum_retries: int = 0
    _maximum_processes: int = 0
    _maximum_subscribers: int = 0
    _maximum_cost_microusd: int = 0
    _violations: list[str] | None = None

    def __post_init__(self) -> None:
        self._violations = []

    def add(self, sample: SoakSample) -> None:
        if self._sample_count >= MAXIMUM_SOAK_SAMPLES:
            raise ValueError("soak sample capacity is exhausted")
        if (
            self._last_observed_at is not None
            and sample.observed_at <= self._last_observed_at
        ):
            raise ValueError("soak samples must have increasing timestamps")
        if sample.elapsed_seconds < self._elapsed_seconds:
            raise ValueError("soak elapsed time must be monotonic")
        self._sample_count += 1
        self._last_observed_at = sample.observed_at
        self._elapsed_seconds = sample.elapsed_seconds
        self._maximum_rss_bytes = max(self._maximum_rss_bytes, sample.rss_bytes)
        self._maximum_queue_depth = max(
            self._maximum_queue_depth,
            sample.queue_depth,
        )
        self._maximum_storage_bytes = max(
            self._maximum_storage_bytes,
            sample.storage_bytes,
        )
        self._maximum_retries = max(self._maximum_retries, sample.retries)
        self._maximum_processes = max(self._maximum_processes, sample.process_count)
        self._maximum_subscribers = max(
            self._maximum_subscribers,
            sample.subscribers,
        )
        self._maximum_cost_microusd = max(
            self._maximum_cost_microusd,
            sample.cost_microusd,
        )
        self._record_violations(sample)

    def summary(self) -> SoakSummary:
        violations = tuple(self._violations or ())
        return SoakSummary(
            sample_count=self._sample_count,
            elapsed_seconds=self._elapsed_seconds,
            maximum_rss_bytes=self._maximum_rss_bytes,
            maximum_queue_depth=self._maximum_queue_depth,
            maximum_storage_bytes=self._maximum_storage_bytes,
            maximum_retries=self._maximum_retries,
            maximum_processes=self._maximum_processes,
            maximum_subscribers=self._maximum_subscribers,
            maximum_cost_microusd=self._maximum_cost_microusd,
            violations=violations,
            complete=(
                self._sample_count > 0
                and self._elapsed_seconds >= self.budget.duration_seconds
                and not violations
            ),
        )

    def _record_violations(self, sample: SoakSample) -> None:
        limits = (
            ("rss_bytes", sample.rss_bytes, self.budget.maximum_rss_bytes),
            ("queue_depth", sample.queue_depth, self.budget.maximum_queue_depth),
            ("storage_bytes", sample.storage_bytes, self.budget.maximum_storage_bytes),
            ("retries", sample.retries, self.budget.maximum_retries),
            ("process_count", sample.process_count, self.budget.maximum_processes),
            ("subscribers", sample.subscribers, self.budget.maximum_subscribers),
            ("cost_microusd", sample.cost_microusd, self.budget.maximum_cost_microusd),
        )
        violations = self._violations
        if violations is None:
            raise RuntimeError("soak accumulator is not initialized")
        for name, value, maximum in limits:
            if value > maximum and name not in violations:
                violations.append(name)
