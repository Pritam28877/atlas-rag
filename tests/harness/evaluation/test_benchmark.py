"""Benchmark summary, regression, and bounded parallelism tests."""

import asyncio

from app.services.harness.evaluation import (
    BenchmarkSample,
    SampleOutcome,
    build_benchmark_report,
    compare_reports,
    run_bounded,
)


def _sample(
    number: int,
    outcome: SampleOutcome = SampleOutcome.PASSED,
) -> BenchmarkSample:
    return BenchmarkSample(
        fixture_sha256=f"{number:064x}",
        outcome=outcome,
        latency_ms=number * 10,
        input_tokens=10,
        output_tokens=5,
        cost_microusd=100,
        peak_rss_bytes=1024,
        trace_sha256=f"{number + 10:064x}",
        failure_reason=None if outcome is SampleOutcome.PASSED else "fixture failed",
    )


def test_report_retains_failures_and_computes_bounded_summary() -> None:
    report = build_benchmark_report(
        harness_revision_sha256="0" * 64,
        model_revision_sha256="1" * 64,
        configuration_sha256="2" * 64,
        samples=(_sample(1), _sample(2, SampleOutcome.FAILED)),
    )
    assert report.summary.passed_count == 1
    assert len(report.failures) == 1
    assert report.summary.latency_p95_ms >= report.summary.latency_median_ms


def test_regression_gate_reports_raw_reasons() -> None:
    baseline = build_benchmark_report(
        harness_revision_sha256="0" * 64,
        model_revision_sha256="1" * 64,
        configuration_sha256="2" * 64,
        samples=(_sample(1), _sample(2)),
    )
    current = build_benchmark_report(
        harness_revision_sha256="0" * 64,
        model_revision_sha256="1" * 64,
        configuration_sha256="2" * 64,
        samples=(_sample(1, SampleOutcome.FAILED), _sample(2, SampleOutcome.FAILED)),
    )
    decision = compare_reports(baseline, current)
    assert not decision.passed
    assert "quality regression" in decision.reasons[0]


def test_parallel_runner_respects_bound() -> None:
    active = 0
    maximum_active = 0

    async def worker(value: int) -> int:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        active -= 1
        return value * 2

    result = asyncio.run(run_bounded((1, 2, 3, 4), worker, maximum_parallelism=2))
    assert result == (2, 4, 6, 8)
    assert maximum_active <= 2
