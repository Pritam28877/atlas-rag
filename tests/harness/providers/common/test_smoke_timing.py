import pytest

from app.cli.harness.smoke_timing import smoke_latency_ms


def test_smoke_latency_is_measured_in_bounded_milliseconds() -> None:
    assert smoke_latency_ms(10.0, 10.025) == 25


def test_smoke_latency_rejects_non_finite_and_reversed_time() -> None:
    with pytest.raises(ValueError, match="finite"):
        smoke_latency_ms(float("nan"), 10.0)
    with pytest.raises(ValueError, match="latency"):
        smoke_latency_ms(10.0, 9.999)
