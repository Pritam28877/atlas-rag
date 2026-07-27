"""Bounded deterministic execution of one vocabulary across adapters."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import aclosing
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from app.services.harness.protocol import ProviderStreamEvent
from app.services.harness.providers.conformance_contracts import (
    CONFORMANCE_SCENARIOS,
    MAXIMUM_CONFORMANCE_ADAPTERS,
    ConformanceAdapterDescriptor,
    ConformanceCaseStatus,
    ConformanceComparison,
    ConformanceComparisonStatus,
    ConformanceFailureCode,
    ConformanceObservation,
    ConformanceOutcome,
    ConformanceReport,
    ConformanceScenario,
)
from app.services.harness.providers.conformance_outcome import (
    ConformanceOutcomeAccumulator,
)
from app.services.harness.providers.provider_stream_control import (
    ProviderStreamControlError,
    ProviderStreamControlErrorCode,
    remaining_seconds,
)


class ConformanceAdapter(Protocol):
    @property
    def descriptor(self) -> ConformanceAdapterDescriptor: ...

    def stream(
        self,
        scenario: ConformanceScenario,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> AsyncGenerator[ProviderStreamEvent, None]: ...


class ConformanceRunnerErrorCode(StrEnum):
    ADAPTERS = "adapters"
    CANCELLED = "cancelled"
    CLOCK = "clock"
    DEADLINE = "deadline"
    SCENARIOS = "scenarios"


class ConformanceRunnerError(RuntimeError):
    def __init__(self, code: ConformanceRunnerErrorCode) -> None:
        super().__init__("provider conformance run failed")
        self.code = code


class BoundedConformanceRunner:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        per_case_timeout_seconds: float = 5.0,
    ) -> None:
        if not 0.1 <= per_case_timeout_seconds <= 60:
            raise ValueError("conformance case timeout is invalid")
        self._clock = clock
        self._case_timeout = per_case_timeout_seconds

    async def run(
        self,
        adapters: Sequence[ConformanceAdapter],
        *,
        scenarios: tuple[ConformanceScenario, ...] = (
            CONFORMANCE_SCENARIOS
        ),
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> ConformanceReport:
        selected_adapters = _canonical_adapters(adapters)
        selected_scenarios = _canonical_scenarios(scenarios)
        _require_utc(deadline_at)
        observations: list[ConformanceObservation] = []
        for scenario in selected_scenarios:
            for adapter in selected_adapters:
                self._require_active(cancellation, deadline_at)
                observations.append(
                    await self._run_case(
                        adapter,
                        scenario,
                        cancellation=cancellation,
                        deadline_at=deadline_at,
                    )
                )
        comparisons = tuple(
            _compare_scenario(
                scenario,
                selected_adapters,
                tuple(observations),
            )
            for scenario in selected_scenarios
        )
        self._require_active(cancellation, deadline_at)
        observed_at = self._clock()
        _require_utc(observed_at)
        return ConformanceReport(
            scenarios=selected_scenarios,
            adapters=tuple(
                adapter.descriptor for adapter in selected_adapters
            ),
            observations=tuple(observations),
            comparisons=comparisons,
            observed_at=observed_at,
        )

    async def _run_case(
        self,
        adapter: ConformanceAdapter,
        scenario: ConformanceScenario,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> ConformanceObservation:
        descriptor = adapter.descriptor
        if scenario not in descriptor.supported_scenarios:
            return _observation(
                descriptor,
                scenario,
                ConformanceCaseStatus.UNSUPPORTED,
            )
        accumulator = ConformanceOutcomeAccumulator()
        timeout_seconds = min(
            self._case_timeout,
            _remaining(self._clock, deadline_at),
        )
        case_task = asyncio.create_task(
            self._consume_case(
                adapter,
                scenario,
                accumulator,
                cancellation=cancellation,
                deadline_at=deadline_at,
            )
        )
        cancellation_task = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                (case_task, cancellation_task),
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task in done:
                raise ConformanceRunnerError(
                    ConformanceRunnerErrorCode.CANCELLED
                )
            if case_task not in done:
                return _failed(
                    descriptor,
                    scenario,
                    ConformanceFailureCode.TIMEOUT,
                )
            outcome = case_task.result()
        except ConformanceRunnerError:
            raise
        except ValueError:
            return _failed(
                descriptor,
                scenario,
                ConformanceFailureCode.CONTRACT,
            )
        except Exception:
            return _failed(
                descriptor,
                scenario,
                ConformanceFailureCode.EXECUTION,
            )
        finally:
            await _cancel_task(cancellation_task)
            await _cancel_task(case_task)
        return _observation(
            descriptor,
            scenario,
            ConformanceCaseStatus.PASSED,
            outcome=outcome,
        )

    async def _consume_case(
        self,
        adapter: ConformanceAdapter,
        scenario: ConformanceScenario,
        accumulator: ConformanceOutcomeAccumulator,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> ConformanceOutcome:
        stream = adapter.stream(
            scenario,
            cancellation=cancellation,
            deadline_at=deadline_at,
        )
        async with aclosing(stream):
            async for event in stream:
                if cancellation.is_set():
                    raise ConformanceRunnerError(
                        ConformanceRunnerErrorCode.CANCELLED
                    )
                accumulator.consume(event)
        return accumulator.finish()

    def _require_active(
        self,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> None:
        if cancellation.is_set():
            raise ConformanceRunnerError(
                ConformanceRunnerErrorCode.CANCELLED
            )
        try:
            remaining_seconds(self._clock, deadline_at)
        except ProviderStreamControlError as error:
            if error.code is ProviderStreamControlErrorCode.DEADLINE:
                raise ConformanceRunnerError(
                    ConformanceRunnerErrorCode.DEADLINE
                ) from None
            raise


def _canonical_adapters(
    adapters: Sequence[ConformanceAdapter],
) -> tuple[ConformanceAdapter, ...]:
    if not 1 <= len(adapters) <= MAXIMUM_CONFORMANCE_ADAPTERS:
        raise ConformanceRunnerError(ConformanceRunnerErrorCode.ADAPTERS)
    selected = tuple(
        sorted(adapters, key=lambda adapter: adapter.descriptor.provider)
    )
    providers = tuple(
        adapter.descriptor.provider for adapter in selected
    )
    if len(set(providers)) != len(providers):
        raise ConformanceRunnerError(ConformanceRunnerErrorCode.ADAPTERS)
    return selected


def _canonical_scenarios(
    scenarios: tuple[ConformanceScenario, ...],
) -> tuple[ConformanceScenario, ...]:
    if (
        not 1 <= len(scenarios) <= len(CONFORMANCE_SCENARIOS)
        or tuple(sorted(set(scenarios))) != scenarios
    ):
        raise ConformanceRunnerError(ConformanceRunnerErrorCode.SCENARIOS)
    return scenarios


def _observation(
    descriptor: ConformanceAdapterDescriptor,
    scenario: ConformanceScenario,
    status: ConformanceCaseStatus,
    *,
    outcome: ConformanceOutcome | None = None,
    failure_code: ConformanceFailureCode | None = None,
) -> ConformanceObservation:
    return ConformanceObservation(
        provider=descriptor.provider,
        adapter_revision_sha256=descriptor.adapter_revision_sha256,
        scenario=scenario,
        status=status,
        outcome=outcome,
        failure_code=failure_code,
    )


def _failed(
    descriptor: ConformanceAdapterDescriptor,
    scenario: ConformanceScenario,
    failure_code: ConformanceFailureCode,
) -> ConformanceObservation:
    return _observation(
        descriptor,
        scenario,
        ConformanceCaseStatus.FAILED,
        failure_code=failure_code,
    )


def _compare_scenario(
    scenario: ConformanceScenario,
    adapters: tuple[ConformanceAdapter, ...],
    observations: tuple[ConformanceObservation, ...],
) -> ConformanceComparison:
    eligible = tuple(
        adapter.descriptor.provider
        for adapter in adapters
        if scenario in adapter.descriptor.supported_scenarios
    )
    if len(eligible) < 2:
        return ConformanceComparison(
            scenario=scenario,
            status=ConformanceComparisonStatus.INSUFFICIENT,
            eligible_providers=eligible,
            reference_sha256=None,
            mismatched_providers=(),
        )
    selected = tuple(
        observation
        for observation in observations
        if (
            observation.scenario is scenario
            and observation.provider in eligible
        )
    )
    reference = next(
        (
            observation.outcome.equivalence_sha256
            for observation in selected
            if observation.outcome is not None
        ),
        None,
    )
    mismatched = tuple(
        observation.provider
        for observation in selected
        if (
            observation.outcome is None
            or reference is None
            or observation.outcome.equivalence_sha256 != reference
        )
    )
    status = (
        ConformanceComparisonStatus.EQUIVALENT
        if not mismatched and reference is not None
        else ConformanceComparisonStatus.MISMATCH
    )
    return ConformanceComparison(
        scenario=scenario,
        status=status,
        eligible_providers=eligible,
        reference_sha256=reference,
        mismatched_providers=mismatched,
    )


def _remaining(
    clock: Callable[[], datetime],
    deadline_at: datetime,
) -> float:
    try:
        return remaining_seconds(clock, deadline_at)
    except ProviderStreamControlError:
        raise ConformanceRunnerError(
            ConformanceRunnerErrorCode.DEADLINE
        ) from None


async def _cancel_task[TaskResult](
    task: asyncio.Task[TaskResult],
) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ConformanceRunnerError(
            ConformanceRunnerErrorCode.CLOCK
        )
