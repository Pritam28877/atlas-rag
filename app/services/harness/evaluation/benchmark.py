"""Bounded benchmark samples, confidence summaries, and regression gates."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol import Sha256, StrictProtocolModel
from app.services.harness.protocol.base import BoundedReason

MAXIMUM_BENCHMARK_SAMPLES = 10_000
MAXIMUM_BENCHMARK_FAILURES = 10_000


class SampleOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class BenchmarkSample(StrictProtocolModel):
    fixture_sha256: Sha256
    outcome: SampleOutcome
    latency_ms: int = Field(ge=0, le=3_600_000)
    input_tokens: int = Field(ge=0, le=2_000_000)
    output_tokens: int = Field(ge=0, le=512_000)
    cost_microusd: int = Field(ge=0, le=10_000_000_000)
    peak_rss_bytes: int = Field(ge=0, le=64 * 1024**3)
    trace_sha256: Sha256
    failure_reason: BoundedReason | None = None

    @model_validator(mode="after")
    def validate_failure(self) -> Self:
        if (self.outcome is SampleOutcome.FAILED) != (
            self.failure_reason is not None
        ):
            raise ValueError("failed samples require failure evidence")
        return self


class BenchmarkFailure(StrictProtocolModel):
    fixture_sha256: Sha256
    reason: BoundedReason


class BenchmarkSummary(StrictProtocolModel):
    sample_count: int = Field(ge=1, le=MAXIMUM_BENCHMARK_SAMPLES)
    passed_count: int = Field(ge=0, le=MAXIMUM_BENCHMARK_SAMPLES)
    quality_ppm: int = Field(ge=0, le=1_000_000)
    quality_lower_ppm: int = Field(ge=0, le=1_000_000)
    quality_upper_ppm: int = Field(ge=0, le=1_000_000)
    latency_median_ms: int = Field(ge=0)
    latency_p95_ms: int = Field(ge=0)
    total_input_tokens: int = Field(ge=0)
    total_output_tokens: int = Field(ge=0)
    total_cost_microusd: int = Field(ge=0)
    peak_rss_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.passed_count > self.sample_count:
            raise ValueError("passed count exceeds sample count")
        if not self.quality_lower_ppm <= self.quality_ppm <= self.quality_upper_ppm:
            raise ValueError("quality confidence interval is invalid")
        if self.latency_median_ms > self.latency_p95_ms:
            raise ValueError("latency quantiles are invalid")
        return self


class BenchmarkReport(StrictProtocolModel):
    harness_revision_sha256: Sha256
    model_revision_sha256: Sha256
    configuration_sha256: Sha256
    samples: tuple[BenchmarkSample, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_BENCHMARK_SAMPLES,
    )
    failures: tuple[BenchmarkFailure, ...] = Field(
        max_length=MAXIMUM_BENCHMARK_FAILURES
    )
    summary: BenchmarkSummary

    @model_validator(mode="after")
    def validate_failures(self) -> Self:
        expected_failures = tuple(
            BenchmarkFailure(
                fixture_sha256=sample.fixture_sha256,
                reason=sample.failure_reason or "sample failed without a reason",
            )
            for sample in self.samples
            if sample.outcome is SampleOutcome.FAILED
        )
        if expected_failures != self.failures:
            raise ValueError("report must retain every raw sample failure")
        if self.summary.sample_count != len(self.samples):
            raise ValueError("summary sample count does not match samples")
        return self


class RegressionDecision(StrictProtocolModel):
    passed: bool
    reasons: tuple[BoundedReason, ...] = Field(max_length=16)


def build_benchmark_report(
    *,
    harness_revision_sha256: Sha256,
    model_revision_sha256: Sha256,
    configuration_sha256: Sha256,
    samples: Sequence[BenchmarkSample],
) -> BenchmarkReport:
    if not 1 <= len(samples) <= MAXIMUM_BENCHMARK_SAMPLES:
        raise ValueError("benchmark sample count exceeds the configured bound")
    sample_tuple = tuple(samples)
    passed_count = sum(
        sample.outcome is SampleOutcome.PASSED for sample in sample_tuple
    )
    quality_ppm = passed_count * 1_000_000 // len(sample_tuple)
    margin = int(
        1.96
        * math.sqrt(
            max(quality_ppm / 1_000_000 * (1 - quality_ppm / 1_000_000), 0)
            / len(sample_tuple)
        )
        * 1_000_000
    )
    summary = BenchmarkSummary(
        sample_count=len(sample_tuple),
        passed_count=passed_count,
        quality_ppm=quality_ppm,
        quality_lower_ppm=max(0, quality_ppm - margin),
        quality_upper_ppm=min(1_000_000, quality_ppm + margin),
        latency_median_ms=_quantile(
            tuple(sample.latency_ms for sample in sample_tuple), 0.50
        ),
        latency_p95_ms=_quantile(
            tuple(sample.latency_ms for sample in sample_tuple), 0.95
        ),
        total_input_tokens=sum(sample.input_tokens for sample in sample_tuple),
        total_output_tokens=sum(sample.output_tokens for sample in sample_tuple),
        total_cost_microusd=sum(sample.cost_microusd for sample in sample_tuple),
        peak_rss_bytes=max(sample.peak_rss_bytes for sample in sample_tuple),
    )
    failures = tuple(
        BenchmarkFailure(
            fixture_sha256=sample.fixture_sha256,
            reason=sample.failure_reason or "sample failed without a reason",
        )
        for sample in sample_tuple
        if sample.outcome is SampleOutcome.FAILED
    )
    return BenchmarkReport(
        harness_revision_sha256=harness_revision_sha256,
        model_revision_sha256=model_revision_sha256,
        configuration_sha256=configuration_sha256,
        samples=sample_tuple,
        failures=failures,
        summary=summary,
    )


def compare_reports(
    baseline: BenchmarkReport,
    current: BenchmarkReport,
    *,
    maximum_quality_drop_ppm: int = 50_000,
    maximum_latency_increase_ppm: int = 200_000,
    maximum_rss_increase_ppm: int = 200_000,
) -> RegressionDecision:
    reasons: list[str] = []
    baseline_quality = baseline.summary.quality_ppm
    if baseline_quality - current.summary.quality_ppm > maximum_quality_drop_ppm:
        reasons.append("quality regression exceeds threshold")
    if _increase_ppm(
        baseline.summary.latency_p95_ms,
        current.summary.latency_p95_ms,
    ) > maximum_latency_increase_ppm:
        reasons.append("latency regression exceeds threshold")
    if _increase_ppm(
        baseline.summary.peak_rss_bytes,
        current.summary.peak_rss_bytes,
    ) > maximum_rss_increase_ppm:
        reasons.append("peak RSS regression exceeds threshold")
    return RegressionDecision(passed=not reasons, reasons=tuple(reasons))


async def run_bounded[SampleT, ResultT](
    values: Sequence[SampleT],
    worker: Callable[[SampleT], Awaitable[ResultT]],
    *,
    maximum_parallelism: int = 4,
) -> tuple[ResultT, ...]:
    if not 1 <= maximum_parallelism <= 64:
        raise ValueError("evaluation parallelism is outside the configured bound")
    semaphore = asyncio.Semaphore(maximum_parallelism)

    async def run_one(value: SampleT) -> ResultT:
        async with semaphore:
            return await worker(value)

    return tuple(await asyncio.gather(*(run_one(value) for value in values)))


def _quantile(values: tuple[int, ...], quantile: float) -> int:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * quantile))
    return ordered[index]


def _increase_ppm(baseline: int, current: int) -> int:
    if baseline <= 0:
        return 0 if current <= 0 else 1_000_000
    return max(0, (current - baseline) * 1_000_000 // baseline)
