from datetime import UTC, datetime, timedelta

from app.services.harness.protocol import (
    ProviderFailureClass,
    ProviderRetryBudget,
    ProviderRetryDecisionCode,
    ProviderRetryDisposition,
    ProviderTransportFailure,
)
from app.services.harness.providers import plan_provider_retry

NOW = datetime(2026, 7, 27, 17, 0, tzinfo=UTC)


def budget() -> ProviderRetryBudget:
    return ProviderRetryBudget(
        max_attempts=3,
        max_wall_time_ms=5_000,
        base_delay_ms=100,
        max_delay_ms=1_000,
    )


def failure(
    *,
    disposition: ProviderRetryDisposition = ProviderRetryDisposition.ELIGIBLE,
    ambiguous: bool = False,
    retry_after_ms: int | None = None,
) -> ProviderTransportFailure:
    return ProviderTransportFailure(
        failure_class=ProviderFailureClass.TRANSIENT,
        retry_disposition=disposition,
        code="provider_unavailable",
        reason="Synthetic retry planner failure.",
        provider_request_sha256="1" * 64,
        retry_after_ms=retry_after_ms,
        ambiguous=ambiguous,
        occurred_at=NOW,
    )


def decision(
    transport_failure: ProviderTransportFailure,
    *,
    current_attempt: int = 1,
    decided_at: datetime = NOW,
):
    return plan_provider_retry(
        transport_failure,
        budget(),
        current_attempt=current_attempt,
        started_at=NOW,
        deadline_at=NOW + timedelta(seconds=10),
        decided_at=decided_at,
    )


def test_retry_delay_is_deterministic_bounded_and_honors_hint() -> None:
    first = decision(failure())
    replay = decision(failure())
    hinted = decision(failure(retry_after_ms=500))
    excessive_hint = decision(failure(retry_after_ms=1_001))

    assert first == replay
    assert first.retry
    assert first.next_attempt == 2
    assert 50 <= first.delay_ms <= 100
    assert hinted.delay_ms == 500
    assert excessive_hint.code is (
        ProviderRetryDecisionCode.WALL_TIME_EXHAUSTED
    )
    assert not excessive_hint.retry


def test_ambiguity_and_prohibited_disposition_never_retry() -> None:
    ambiguous = decision(
        failure(
            disposition=ProviderRetryDisposition.PROHIBITED,
            ambiguous=True,
        )
    )
    prohibited = decision(
        failure(disposition=ProviderRetryDisposition.PROHIBITED)
    )

    assert ambiguous.code is ProviderRetryDecisionCode.AMBIGUOUS
    assert prohibited.code is ProviderRetryDecisionCode.RETRY_PROHIBITED
    assert not ambiguous.retry
    assert not prohibited.retry


def test_attempt_and_original_wall_time_budgets_cannot_reset() -> None:
    attempts = decision(failure(), current_attempt=3)
    elapsed = decision(
        failure(),
        current_attempt=1,
        decided_at=NOW + timedelta(milliseconds=4_950),
    )

    assert attempts.code is ProviderRetryDecisionCode.ATTEMPTS_EXHAUSTED
    assert elapsed.code is ProviderRetryDecisionCode.WALL_TIME_EXHAUSTED
    assert not attempts.retry
    assert not elapsed.retry
