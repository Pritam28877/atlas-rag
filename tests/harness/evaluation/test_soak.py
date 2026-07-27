"""Bounded soak accumulator tests."""

from datetime import UTC, datetime, timedelta

import pytest

from app.services.harness.evaluation.soak import (
    SoakAccumulator,
    SoakBudget,
    SoakSample,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _budget() -> SoakBudget:
    return SoakBudget(
        duration_seconds=120,
        sample_interval_seconds=60,
        maximum_rss_bytes=100,
        maximum_queue_depth=10,
        maximum_storage_bytes=1_000,
        maximum_retries=2,
        maximum_processes=4,
        maximum_subscribers=4,
        maximum_cost_microusd=10,
    )


def _sample(second: int, *, rss_bytes: int = 10) -> SoakSample:
    return SoakSample(
        observed_at=NOW + timedelta(seconds=second),
        elapsed_seconds=second,
        rss_bytes=rss_bytes,
        queue_depth=1,
        storage_bytes=10,
        retries=0,
        process_count=1,
        subscribers=1,
        cost_microusd=1,
    )


def test_soak_summary_is_bounded_and_completes_only_with_clean_window() -> None:
    accumulator = SoakAccumulator(_budget())
    accumulator.add(_sample(0))
    accumulator.add(_sample(120))
    summary = accumulator.summary()
    assert summary.complete
    assert summary.sample_count == 2
    assert summary.violations == ()


def test_soak_records_resource_violation_without_growing_sample_memory() -> None:
    accumulator = SoakAccumulator(_budget())
    accumulator.add(_sample(0, rss_bytes=101))
    accumulator.add(_sample(120))
    summary = accumulator.summary()
    assert not summary.complete
    assert summary.violations == ("rss_bytes",)
    assert summary.maximum_rss_bytes == 101


def test_soak_rejects_out_of_order_samples() -> None:
    accumulator = SoakAccumulator(_budget())
    accumulator.add(_sample(60))
    with pytest.raises(ValueError, match="increasing"):
        accumulator.add(_sample(60))
