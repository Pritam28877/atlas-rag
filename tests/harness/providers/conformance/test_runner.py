import asyncio
from datetime import timedelta

import pytest

from app.services.harness.protocol import (
    ProviderCompleted,
    ProviderFinishReason,
    ProviderTextDelta,
)
from app.services.harness.providers import (
    ConformanceCaseStatus,
    ConformanceComparisonStatus,
    ConformanceFailureCode,
    ConformanceScenario,
)
from app.services.harness.providers.conformance_runner import (
    BoundedConformanceRunner,
    ConformanceRunnerError,
    ConformanceRunnerErrorCode,
)
from tests.harness.providers.conformance.fixtures import (
    NOW,
    StaticConformanceAdapter,
    blocked_stream,
    text_events,
    tool_events,
)


def test_equivalence_ignores_delta_boundaries_usage_and_call_ids() -> None:
    first = StaticConformanceAdapter(
        "mock",
        {
            ConformanceScenario.TEXT_STREAM: text_events(
                "same ",
                "answer",
                input_tokens=2,
            ),
            ConformanceScenario.TOOL_CALLS: tool_events("call_mock"),
        },
    )
    second = StaticConformanceAdapter(
        "openai",
        {
            ConformanceScenario.TEXT_STREAM: text_events(
                "same answer",
                input_tokens=11,
            ),
            ConformanceScenario.TOOL_CALLS: tool_events("call_openai"),
        },
    )

    report = _run(
        (second, first),
        (
            ConformanceScenario.TEXT_STREAM,
            ConformanceScenario.TOOL_CALLS,
        ),
    )

    assert tuple(
        adapter.provider for adapter in report.adapters
    ) == ("mock", "openai")
    assert all(
        observation.status is ConformanceCaseStatus.PASSED
        for observation in report.observations
    )
    assert all(
        comparison.status
        is ConformanceComparisonStatus.EQUIVALENT
        for comparison in report.comparisons
    )
    assert first.closed_streams == second.closed_streams == 2


def test_unsupported_scenario_is_explicit_and_not_executed() -> None:
    supported = StaticConformanceAdapter(
        "mock",
        {ConformanceScenario.TEXT_STREAM: text_events("answer")},
    )
    unsupported = StaticConformanceAdapter("vertex", {})

    report = _run(
        (supported, unsupported),
        (ConformanceScenario.TEXT_STREAM,),
    )

    vertex = report.observations[1]
    assert vertex.provider == "vertex"
    assert vertex.status is ConformanceCaseStatus.UNSUPPORTED
    assert unsupported.calls == []
    assert (
        report.comparisons[0].status
        is ConformanceComparisonStatus.INSUFFICIENT
    )


def test_semantic_drift_and_adapter_failures_are_sanitized() -> None:
    reference = StaticConformanceAdapter(
        "mock",
        {ConformanceScenario.TEXT_STREAM: text_events("expected")},
    )
    drifted = StaticConformanceAdapter(
        "openai",
        {ConformanceScenario.TEXT_STREAM: text_events("different")},
    )
    failed = StaticConformanceAdapter(
        "vertex",
        {
            ConformanceScenario.TEXT_STREAM: RuntimeError(
                "provider secret detail"
            )
        },
    )

    report = _run(
        (reference, drifted, failed),
        (ConformanceScenario.TEXT_STREAM,),
    )

    comparison = report.comparisons[0]
    assert comparison.status is ConformanceComparisonStatus.MISMATCH
    assert comparison.mismatched_providers == ("openai", "vertex")
    vertex = report.observations[2]
    assert vertex.status is ConformanceCaseStatus.FAILED
    assert vertex.failure_code is ConformanceFailureCode.EXECUTION
    assert "provider secret detail" not in report.model_dump_json()


@pytest.mark.parametrize(
    "events",
    (
        (
            ProviderTextDelta(sequence=2, text="gap"),
            ProviderCompleted(
                sequence=3,
                finish_reason=ProviderFinishReason.STOP,
            ),
        ),
        (ProviderTextDelta(sequence=1, text="no terminal"),),
    ),
)
def test_malformed_canonical_stream_is_a_contract_failure(events) -> None:
    adapter = StaticConformanceAdapter(
        "mock",
        {ConformanceScenario.MALFORMED_STREAM: events},
    )

    report = _run(
        (adapter,),
        (ConformanceScenario.MALFORMED_STREAM,),
    )

    observation = report.observations[0]
    assert observation.status is ConformanceCaseStatus.FAILED
    assert observation.failure_code is ConformanceFailureCode.CONTRACT


def test_timeout_and_caller_cancellation_are_bounded() -> None:
    adapter = StaticConformanceAdapter(
        "mock",
        {ConformanceScenario.TEXT_STREAM: blocked_stream},
    )
    runner = BoundedConformanceRunner(
        clock=lambda: NOW,
        per_case_timeout_seconds=0.1,
    )
    report = asyncio.run(
        runner.run(
            (adapter,),
            scenarios=(ConformanceScenario.TEXT_STREAM,),
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=1),
        )
    )
    assert (
        report.observations[0].failure_code
        is ConformanceFailureCode.TIMEOUT
    )

    async def cancel_during_stream() -> None:
        cancellation = asyncio.Event()
        task = asyncio.create_task(
            runner.run(
                (adapter,),
                scenarios=(ConformanceScenario.TEXT_STREAM,),
                cancellation=cancellation,
                deadline_at=NOW + timedelta(seconds=1),
            )
        )
        while len(adapter.calls) < 2:
            await asyncio.sleep(0)
        cancellation.set()
        with pytest.raises(ConformanceRunnerError) as cancelled:
            await asyncio.wait_for(task, timeout=0.5)
        assert (
            cancelled.value.code
            is ConformanceRunnerErrorCode.CANCELLED
        )

    asyncio.run(cancel_during_stream())


def _run(adapters, scenarios):
    runner = BoundedConformanceRunner(clock=lambda: NOW)
    return asyncio.run(
        runner.run(
            adapters,
            scenarios=scenarios,
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=30),
        )
    )
