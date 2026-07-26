"""Deterministic bounded provider retry planning."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from app.services.harness.protocol import (
    ProviderRetryBudget,
    ProviderRetryDecision,
    ProviderRetryDecisionCode,
    ProviderRetryDisposition,
    ProviderTransportFailure,
)


def plan_provider_retry(
    failure: ProviderTransportFailure,
    budget: ProviderRetryBudget,
    *,
    current_attempt: int,
    started_at: datetime,
    deadline_at: datetime,
    decided_at: datetime,
) -> ProviderRetryDecision:
    _require_utc(started_at, "provider retry start")
    _require_utc(deadline_at, "provider retry deadline")
    _require_utc(decided_at, "provider retry decision")
    if current_attempt < 1 or current_attempt > 64:
        raise ValueError("provider retry current attempt is invalid")
    if decided_at < started_at:
        raise ValueError("provider retry decision precedes its start")
    if not started_at <= failure.occurred_at <= decided_at:
        raise ValueError("provider failure time is outside the attempt window")
    if failure.ambiguous:
        return _denied(
            ProviderRetryDecisionCode.AMBIGUOUS,
            "Ambiguous provider response cannot be retried.",
            current_attempt,
            decided_at,
        )
    if failure.retry_disposition is ProviderRetryDisposition.PROHIBITED:
        return _denied(
            ProviderRetryDecisionCode.RETRY_PROHIBITED,
            "Provider failure is not retry eligible.",
            current_attempt,
            decided_at,
        )
    if current_attempt >= budget.max_attempts:
        return _denied(
            ProviderRetryDecisionCode.ATTEMPTS_EXHAUSTED,
            "Provider retry attempt budget is exhausted.",
            current_attempt,
            decided_at,
        )
    if (
        failure.retry_after_ms is not None
        and failure.retry_after_ms > budget.max_delay_ms
    ):
        return _denied(
            ProviderRetryDecisionCode.WALL_TIME_EXHAUSTED,
            "Provider retry delay exceeds the bounded retry budget.",
            current_attempt,
            decided_at,
        )
    delay_ms = _retry_delay_ms(failure, budget, current_attempt)
    wall_deadline = started_at + timedelta(
        milliseconds=budget.max_wall_time_ms
    )
    effective_deadline = min(wall_deadline, deadline_at)
    remaining_ms = int(
        (effective_deadline - decided_at).total_seconds() * 1_000
    )
    if remaining_ms <= delay_ms:
        return _denied(
            ProviderRetryDecisionCode.WALL_TIME_EXHAUSTED,
            "Provider retry wall-time budget is exhausted.",
            current_attempt,
            decided_at,
        )
    return ProviderRetryDecision(
        retry=True,
        code=ProviderRetryDecisionCode.RETRY,
        reason="Provider failure is eligible for bounded retry.",
        current_attempt=current_attempt,
        next_attempt=current_attempt + 1,
        delay_ms=delay_ms,
        decided_at=decided_at,
    )


def _retry_delay_ms(
    failure: ProviderTransportFailure,
    budget: ProviderRetryBudget,
    current_attempt: int,
) -> int:
    exponential = budget.base_delay_ms * (2 ** (current_attempt - 1))
    bounded_exponential = min(exponential, budget.max_delay_ms)
    jitter_span = bounded_exponential // 2
    seed = (
        f"{failure.provider_request_sha256}:{current_attempt}".encode()
    )
    jitter_value = int.from_bytes(
        hashlib.sha256(seed).digest()[:8],
        byteorder="big",
    )
    jittered = jitter_span + (jitter_value % (jitter_span + 1))
    provider_hint: int = (
        0
        if failure.retry_after_ms is None
        else failure.retry_after_ms
    )
    requested_delay: int = max(jittered, provider_hint)
    maximum_delay: int = budget.max_delay_ms
    return min(requested_delay, maximum_delay)


def _denied(
    code: ProviderRetryDecisionCode,
    reason: str,
    current_attempt: int,
    decided_at: datetime,
) -> ProviderRetryDecision:
    return ProviderRetryDecision(
        retry=False,
        code=code,
        reason=reason,
        current_attempt=current_attempt,
        decided_at=decided_at,
    )


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must use UTC")
