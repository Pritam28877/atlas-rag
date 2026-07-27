"""Bounded monotonic timing for redacted live-smoke evidence."""

import math

MAXIMUM_SMOKE_LATENCY_MS = 3_600_000


def smoke_latency_ms(
    started_at: float,
    completed_at: float,
) -> int:
    if not math.isfinite(started_at) or not math.isfinite(completed_at):
        raise ValueError("provider smoke timing must be finite")
    if completed_at < started_at:
        raise ValueError("provider smoke latency is invalid")
    elapsed_ms = int((completed_at - started_at) * 1_000)
    if not 0 <= elapsed_ms <= MAXIMUM_SMOKE_LATENCY_MS:
        raise ValueError("provider smoke latency is invalid")
    return elapsed_ms
