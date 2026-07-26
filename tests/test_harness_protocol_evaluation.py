from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import (
    EvaluationFailure,
    EvaluationMetric,
    EvaluationRunRecord,
    EvaluationState,
    InvalidTransitionError,
    ResourceUsage,
    require_evaluation_transition,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
DIGEST = "0" * 64


def identifier(prefix: str, character: str = "0") -> str:
    return f"{prefix}_{character * 32}"


def resource_usage() -> ResourceUsage:
    return ResourceUsage(
        steps=1,
        tool_calls=0,
        input_tokens=100,
        output_tokens=20,
        tool_output_bytes=0,
        duration_ms=1_000,
        cost_microusd=5_000,
    )


def evaluation(**overrides: object) -> EvaluationRunRecord:
    values: dict[str, object] = {
        "evaluation_id": identifier("evl"),
        "state": EvaluationState.COMPLETED,
        "harness_revision_sha256": DIGEST,
        "model_revision_sha256": "1" * 64,
        "configuration_sha256": "2" * 64,
        "fixture_sha256s": ("3" * 64, "4" * 64),
        "metrics": (
            EvaluationMetric(name="grounded-answer", score_ppm=950_000),
        ),
        "failures": (),
        "usage": resource_usage(),
        "trace_event_ids": (
            identifier("evt", "1"),
            identifier("evt", "2"),
        ),
        "created_at": NOW,
        "started_at": NOW + timedelta(seconds=1),
        "completed_at": NOW + timedelta(seconds=2),
    }
    values.update(overrides)
    return EvaluationRunRecord.model_validate(values)


def test_completed_evaluation_requires_pinned_metrics_and_ordered_evidence() -> None:
    assert evaluation().metrics[0].score_ppm == 950_000

    with pytest.raises(ValidationError, match="fixture hashes"):
        evaluation(fixture_sha256s=("4" * 64, "3" * 64))
    with pytest.raises(ValidationError, match="trace event IDs"):
        evaluation(
            trace_event_ids=(
                identifier("evt", "2"),
                identifier("evt", "1"),
            )
        )
    with pytest.raises(ValidationError, match="at least one metric"):
        evaluation(metrics=())


def test_failed_evaluation_requires_failure_for_a_run_fixture() -> None:
    failure = EvaluationFailure(
        fixture_sha256="3" * 64,
        reason="Expected citation evidence was absent.",
    )
    failed = evaluation(
        state=EvaluationState.FAILED,
        metrics=(),
        failures=(failure,),
    )
    assert failed.failures == (failure,)

    with pytest.raises(ValidationError, match="failure evidence"):
        evaluation(state=EvaluationState.FAILED, metrics=())
    with pytest.raises(ValidationError, match="reference a run fixture"):
        evaluation(
            state=EvaluationState.FAILED,
            metrics=(),
            failures=(
                EvaluationFailure(
                    fixture_sha256="9" * 64,
                    reason="Unknown fixture reference.",
                ),
            ),
        )


def test_pending_cancellation_may_finish_without_starting() -> None:
    cancelled = evaluation(
        state=EvaluationState.CANCELLED,
        metrics=(),
        started_at=None,
        completed_at=NOW + timedelta(seconds=1),
    )

    assert cancelled.started_at is None


def test_evaluation_state_transitions_fail_closed() -> None:
    require_evaluation_transition(
        EvaluationState.PENDING,
        EvaluationState.RUNNING,
    )

    with pytest.raises(InvalidTransitionError):
        require_evaluation_transition(
            EvaluationState.COMPLETED,
            EvaluationState.RUNNING,
        )
