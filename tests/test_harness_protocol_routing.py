from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import (
    DataClassification,
    EvaluationFailure,
    EvaluationMetric,
    EvaluationRunRecord,
    EvaluationState,
    InvalidTransitionError,
    ProviderRequirements,
    ProviderRoute,
    ProviderRouteDecisionRecord,
    RejectedProviderRoute,
    ResourceUsage,
    RouteHealth,
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


def requirements() -> ProviderRequirements:
    return ProviderRequirements(
        input_tokens=10_000,
        reserved_output_tokens=2_000,
        required_capabilities=("reasoning", "tools"),
        data_classification=DataClassification.CONFIDENTIAL,
        allowed_regions=("ap-south-1", "us-east-1"),
        max_retention_days=30,
        allow_training=False,
        max_cost_microusd=100_000,
    )


def route(**overrides: object) -> ProviderRoute:
    values: dict[str, object] = {
        "route_id": "openai.primary",
        "provider": "openai",
        "model": "model-revision",
        "model_revision_sha256": DIGEST,
        "region": "us-east-1",
        "health": RouteHealth.HEALTHY,
        "capabilities": ("reasoning", "tools"),
        "accepted_data_classifications": (
            DataClassification.CONFIDENTIAL,
            DataClassification.INTERNAL,
        ),
        "retention_days": 30,
        "training_enabled": False,
        "destination_sha256": "3" * 64,
        "context_window_tokens": 128_000,
        "max_output_tokens": 16_000,
        "estimated_cost_microusd": 50_000,
        "health_snapshot_sha256": "1" * 64,
        "price_version_sha256": "2" * 64,
    }
    values.update(overrides)
    return ProviderRoute.model_validate(values)


def decision(**overrides: object) -> ProviderRouteDecisionRecord:
    values: dict[str, object] = {
        "provider_decision_id": identifier("pvd"),
        "turn_id": identifier("trn"),
        "requirements": requirements(),
        "eligible_routes": (route(),),
        "rejected_routes": (
            RejectedProviderRoute(
                route_id="bedrock.secondary",
                reason="Required model capability is unavailable.",
            ),
        ),
        "selected_route_id": "openai.primary",
        "selection_reason": "Lowest healthy cost inside the allowed policy.",
        "decided_at": NOW,
    }
    values.update(overrides)
    return ProviderRouteDecisionRecord.model_validate(values)


def test_provider_decision_records_eligible_and_eliminated_routes() -> None:
    record = decision()

    assert record.selected_route_id == "openai.primary"
    assert record.rejected_routes[0].route_id == "bedrock.secondary"
    with pytest.raises(ValidationError, match="unique and sorted"):
        decision(eligible_routes=(route(), route()))
    with pytest.raises(ValidationError, match="eligible and rejected"):
        decision(
            rejected_routes=(
                RejectedProviderRoute(
                    route_id="openai.primary",
                    reason="Injected conflicting classification.",
                ),
            )
        )
    with pytest.raises(ValidationError, match="must be eligible"):
        decision(selected_route_id="local.unlisted")


def test_eligible_provider_route_must_satisfy_every_requirement() -> None:
    with pytest.raises(ValidationError, match="violates requirements"):
        decision(eligible_routes=(route(region="eu-west-1"),))
    with pytest.raises(ValidationError, match="violates requirements"):
        decision(eligible_routes=(route(context_window_tokens=11_999),))
    with pytest.raises(ValidationError, match="violates requirements"):
        decision(eligible_routes=(route(estimated_cost_microusd=100_001),))
    with pytest.raises(ValidationError, match="violates requirements"):
        decision(eligible_routes=(route(capabilities=("reasoning",)),))
    with pytest.raises(ValidationError, match="violates requirements"):
        decision(eligible_routes=(route(training_enabled=True),))
    with pytest.raises(ValidationError, match="violates requirements"):
        decision(eligible_routes=(route(retention_days=31),))


def test_all_rejected_provider_decision_has_no_selected_route() -> None:
    record = decision(
        eligible_routes=(),
        selected_route_id=None,
    )

    assert record.eligible_routes == ()
    with pytest.raises(ValidationError, match="requires a selected route"):
        decision(selected_route_id=None)


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
