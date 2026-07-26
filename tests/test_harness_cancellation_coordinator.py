import asyncio
import time
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.services.harness.sessions import (
    CancellationControlOutcome,
    CancellationCoordinator,
    CancellationCoordinatorError,
    CancellationCoordinatorErrorCode,
    CancellationReport,
    CancellationResourceIdentity,
    CancellationResourceKind,
    CancellationScopeState,
    CancellationSettlement,
)

NOW = datetime(2026, 7, 27, 16, 0, tzinfo=UTC)
TURN_ID = "trn_" + "1" * 32


class EvidenceSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.reports: list[CancellationReport] = []

    async def record_cancellation(self, report: CancellationReport) -> None:
        if self.fail:
            raise RuntimeError("injected evidence failure")
        self.reports.append(report)


class Resource:
    def __init__(
        self,
        number: int,
        kind: CancellationResourceKind,
        *,
        settle_on_cancel: bool = False,
        settle_on_force: bool = False,
        cancel_fails: bool = False,
        force_fails: bool = False,
    ) -> None:
        self._identity = CancellationResourceIdentity(
            resource_id=f"{number:064x}",
            kind=kind,
        )
        self._settle_on_cancel = settle_on_cancel
        self._settle_on_force = settle_on_force
        self._cancel_fails = cancel_fails
        self._force_fails = force_fails
        self._settled = asyncio.Event()
        self.cancel_calls = 0
        self.force_calls = 0
        self.waiters = 0

    @property
    def cancellation_identity(self) -> CancellationResourceIdentity:
        return self._identity

    def request_cancel(self) -> None:
        self.cancel_calls += 1
        if self._cancel_fails:
            raise RuntimeError("injected cancel failure")
        if self._settle_on_cancel:
            self._settled.set()

    def force_terminate(self) -> None:
        self.force_calls += 1
        if self._force_fails:
            raise RuntimeError("injected force failure")
        if self._settle_on_force:
            self._settled.set()

    async def wait_settled(self) -> None:
        self.waiters += 1
        try:
            await self._settled.wait()
        finally:
            self.waiters -= 1


def test_cooperative_and_forced_resources_settle_without_remaining_owner() -> None:
    async def scenario() -> None:
        sink = EvidenceSink()
        provider = Resource(
            1,
            CancellationResourceKind.PROVIDER_REQUEST,
            settle_on_cancel=True,
        )
        process = Resource(
            2,
            CancellationResourceKind.PROCESS_TREE,
            settle_on_force=True,
        )
        coordinator = CancellationCoordinator(
            TURN_ID,
            sink,
            maximum_resources=2,
            clock=lambda: NOW,
        )
        await coordinator.register(provider)
        await coordinator.register(process)

        report = await coordinator.cancel(
            "User cancelled the turn.",
            evidence_timeout_seconds=0.1,
            cooperative_settle_timeout_seconds=0.01,
            forced_settle_timeout_seconds=0.1,
        )
        replay = await coordinator.cancel("duplicate cancellation")

        assert report.state is CancellationScopeState.CANCELLED
        assert replay == report
        assert coordinator.cancellation_event.is_set()
        assert await coordinator.active_resources() == 0
        assert await coordinator.state() is CancellationScopeState.CANCELLED
        assert sink.reports == [report]
        assert provider.cancel_calls == 1
        assert provider.force_calls == 0
        assert process.cancel_calls == 1
        assert process.force_calls == 1
        assert provider.waiters == process.waiters == 0
        settlements = {
            result.identity.kind: result.settlement
            for result in report.resources
        }
        assert settlements == {
            CancellationResourceKind.PROCESS_TREE: (
                CancellationSettlement.AFTER_ESCALATION
            ),
            CancellationResourceKind.PROVIDER_REQUEST: (
                CancellationSettlement.COOPERATIVE
            ),
        }

    asyncio.run(scenario())


def test_uncontained_resource_is_retained_with_operator_evidence() -> None:
    async def scenario() -> None:
        sink = EvidenceSink()
        resource = Resource(1, CancellationResourceKind.PROCESS_TREE)
        coordinator = CancellationCoordinator(
            TURN_ID,
            sink,
            clock=lambda: NOW,
        )
        await coordinator.register(resource)
        started = time.monotonic()

        report = await coordinator.cancel(
            "Cancellation containment test.",
            evidence_timeout_seconds=0.05,
            cooperative_settle_timeout_seconds=0.01,
            forced_settle_timeout_seconds=0.01,
        )
        elapsed = time.monotonic() - started

        assert elapsed < 0.2
        assert report.state is CancellationScopeState.NEEDS_OPERATOR
        assert report.resources[0].settlement is (
            CancellationSettlement.CONTAINMENT_FAILED
        )
        assert await coordinator.active_resources() == 1
        assert await coordinator.state() is CancellationScopeState.NEEDS_OPERATOR
        assert sink.reports == [report]
        assert resource.cancel_calls == resource.force_calls == 1
        assert resource.waiters == 0

    asyncio.run(scenario())


def test_control_failure_is_explicit_even_when_force_settles() -> None:
    async def scenario() -> None:
        sink = EvidenceSink()
        resource = Resource(
            1,
            CancellationResourceKind.PROVIDER_REQUEST,
            cancel_fails=True,
            settle_on_force=True,
        )
        coordinator = CancellationCoordinator(
            TURN_ID,
            sink,
            clock=lambda: NOW,
        )
        await coordinator.register(resource)

        report = await coordinator.cancel(
            "Force provider transport closed.",
            cooperative_settle_timeout_seconds=0.01,
        )
        result = report.resources[0]

        assert report.state is CancellationScopeState.CANCELLED
        assert result.cancel_request is CancellationControlOutcome.FAILED
        assert result.force_request is CancellationControlOutcome.SUCCEEDED
        assert result.settlement is CancellationSettlement.AFTER_ESCALATION
        assert await coordinator.active_resources() == 0

    asyncio.run(scenario())


def test_registration_is_bounded_and_release_checks_ownership() -> None:
    async def scenario() -> None:
        coordinator = CancellationCoordinator(
            TURN_ID,
            EvidenceSink(),
            maximum_resources=1,
            clock=lambda: NOW,
        )
        first = Resource(1, CancellationResourceKind.PROVIDER_REQUEST)
        duplicate = Resource(1, CancellationResourceKind.PROCESS_TREE)
        second = Resource(2, CancellationResourceKind.PROCESS_TREE)
        await coordinator.register(first)
        await coordinator.register(first)

        with pytest.raises(CancellationCoordinatorError) as duplicate_error:
            await coordinator.register(duplicate)
        with pytest.raises(CancellationCoordinatorError) as capacity_error:
            await coordinator.register(second)
        await coordinator.release(first)

        assert duplicate_error.value.code is (
            CancellationCoordinatorErrorCode.DUPLICATE_RESOURCE
        )
        assert capacity_error.value.code is (
            CancellationCoordinatorErrorCode.CAPACITY
        )
        assert await coordinator.active_resources() == 0

    asyncio.run(scenario())


def test_evidence_failure_prevents_terminal_cancelled_state() -> None:
    async def scenario() -> None:
        resource = Resource(
            1,
            CancellationResourceKind.PROVIDER_REQUEST,
            settle_on_cancel=True,
        )
        coordinator = CancellationCoordinator(
            TURN_ID,
            EvidenceSink(fail=True),
            clock=lambda: NOW,
        )
        await coordinator.register(resource)

        with pytest.raises(CancellationCoordinatorError) as evidence_error:
            await coordinator.cancel(
                "Evidence sink failure.",
                evidence_timeout_seconds=0.05,
            )

        assert evidence_error.value.code is (
            CancellationCoordinatorErrorCode.EVIDENCE_FAILED
        )
        assert await coordinator.state() is CancellationScopeState.NEEDS_OPERATOR
        assert await coordinator.active_resources() == 0
        report = await coordinator.last_report()
        assert report is not None
        assert report.state is CancellationScopeState.CANCELLED

    asyncio.run(scenario())


def test_outer_cancellation_waits_for_owned_containment() -> None:
    async def scenario() -> None:
        sink = EvidenceSink()
        resource = Resource(1, CancellationResourceKind.PROCESS_TREE)
        coordinator = CancellationCoordinator(
            TURN_ID,
            sink,
            clock=lambda: NOW,
        )
        await coordinator.register(resource)
        cancellation_task = asyncio.create_task(
            coordinator.cancel(
                "Caller disappeared during cancellation.",
                evidence_timeout_seconds=0.05,
                cooperative_settle_timeout_seconds=0.01,
                forced_settle_timeout_seconds=0.01,
            )
        )
        await asyncio.sleep(0)

        cancellation_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancellation_task

        assert await coordinator.state() is (
            CancellationScopeState.NEEDS_OPERATOR
        )
        assert await coordinator.active_resources() == 1
        assert len(sink.reports) == 1
        assert sink.reports[0].resources[0].settlement is (
            CancellationSettlement.CONTAINMENT_FAILED
        )
        assert resource.waiters == 0

    asyncio.run(scenario())


def test_invalid_inputs_do_not_start_cancellation() -> None:
    async def scenario() -> None:
        coordinator = CancellationCoordinator(
            TURN_ID,
            EvidenceSink(),
            clock=lambda: NOW,
        )

        with pytest.raises(ValidationError):
            await coordinator.cancel("")

        assert await coordinator.state() is CancellationScopeState.OPEN
        assert not coordinator.cancellation_event.is_set()

        invalid_clock_coordinator = CancellationCoordinator(
            TURN_ID,
            EvidenceSink(),
            clock=lambda: datetime(2026, 7, 27, 16, 0),
        )

        with pytest.raises(ValueError, match="must return UTC"):
            await invalid_clock_coordinator.cancel("Valid reason.")

        assert await invalid_clock_coordinator.state() is (
            CancellationScopeState.OPEN
        )
        assert not invalid_clock_coordinator.cancellation_event.is_set()

    asyncio.run(scenario())
