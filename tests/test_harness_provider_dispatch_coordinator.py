import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.harness.journal import SQLiteProviderCostLedger
from app.services.harness.protocol import (
    ProviderCostLimits,
    ProviderFailureClass,
    ProviderRetryBudget,
    ProviderRetryDisposition,
    ProviderTransportError,
    ProviderTransportFailure,
)
from app.services.harness.providers import (
    ProviderAttemptSuccess,
    ProviderDispatchCoordinator,
    ProviderDispatchError,
    ProviderDispatchErrorCode,
    ProviderDispatchRequest,
)

NOW = datetime(2026, 7, 27, 18, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Delay:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.delays: list[int] = []

    async def wait(
        self,
        delay_ms: int,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> None:
        self.delays.append(delay_ms)
        self.clock.now += timedelta(milliseconds=delay_ms)


class Executor:
    def __init__(self, failures: list[ProviderTransportFailure]) -> None:
        self.failures = failures
        self.attempts: list[int] = []

    async def execute(
        self,
        attempt: int,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> ProviderAttemptSuccess[str]:
        self.attempts.append(attempt)
        if self.failures:
            raise ProviderTransportError(self.failures.pop(0))
        return ProviderAttemptSuccess(
            value="completed",
            actual_cost_microusd=30,
        )


def dispatch_request(
    *,
    max_turn_microusd: int = 200,
) -> ProviderDispatchRequest:
    return ProviderDispatchRequest(
        workspace_id="wsp_" + "1" * 32,
        turn_id="trn_" + "2" * 32,
        request_id="req_" + "3" * 32,
        provider_request_sha256="4" * 64,
        estimated_attempt_cost_microusd=50,
        cost_limits=ProviderCostLimits(
            max_call_microusd=50,
            max_turn_microusd=max_turn_microusd,
            max_workspace_microusd=200,
        ),
        retry_budget=ProviderRetryBudget(
            max_attempts=3,
            max_wall_time_ms=5_000,
            base_delay_ms=1,
            max_delay_ms=10,
        ),
        deadline_at=NOW + timedelta(seconds=10),
    )


def failure(
    *,
    ambiguous: bool = False,
    request_sha256: str = "4" * 64,
) -> ProviderTransportFailure:
    return ProviderTransportFailure(
        failure_class=ProviderFailureClass.TRANSIENT,
        retry_disposition=(
            ProviderRetryDisposition.PROHIBITED
            if ambiguous
            else ProviderRetryDisposition.ELIGIBLE
        ),
        code="provider_unavailable",
        reason="Synthetic dispatch failure.",
        provider_request_sha256=request_sha256,
        ambiguous=ambiguous,
        occurred_at=NOW,
    )


def test_retry_attempts_share_cost_and_original_time_budgets(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        ledger = await SQLiteProviderCostLedger.open(
            private_database(tmp_path)
        )
        clock = Clock()
        delay = Delay(clock)
        executor = Executor([failure()])
        coordinator = ProviderDispatchCoordinator(
            ledger,
            delay,
            clock=clock,
        )
        try:
            value = await coordinator.dispatch(
                dispatch_request(),
                executor,
                cancellation=asyncio.Event(),
            )
            snapshot = await ledger.snapshot(
                "wsp_" + "1" * 32,
                "trn_" + "2" * 32,
                observed_at=clock(),
            )
        finally:
            await ledger.close()

        assert value == "completed"
        assert executor.attempts == [1, 2]
        assert len(delay.delays) == 1
        assert snapshot.turn_settled_microusd == 80
        assert snapshot.active_reservations == 0

    asyncio.run(scenario())


def test_cost_cap_stops_retry_before_second_dispatch(tmp_path: Path) -> None:
    async def scenario() -> None:
        ledger = await SQLiteProviderCostLedger.open(
            private_database(tmp_path)
        )
        clock = Clock()
        executor = Executor([failure()])
        coordinator = ProviderDispatchCoordinator(
            ledger,
            Delay(clock),
            clock=clock,
        )
        try:
            with pytest.raises(ProviderDispatchError) as error:
                await coordinator.dispatch(
                    dispatch_request(max_turn_microusd=50),
                    executor,
                    cancellation=asyncio.Event(),
                )
        finally:
            await ledger.close()

        assert error.value.code is ProviderDispatchErrorCode.COST
        assert executor.attempts == [1]

    asyncio.run(scenario())


def test_ambiguous_attempt_is_settled_and_never_retried(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        ledger = await SQLiteProviderCostLedger.open(
            private_database(tmp_path)
        )
        clock = Clock()
        executor = Executor([failure(ambiguous=True)])
        coordinator = ProviderDispatchCoordinator(
            ledger,
            Delay(clock),
            clock=clock,
        )
        try:
            with pytest.raises(ProviderDispatchError) as error:
                await coordinator.dispatch(
                    dispatch_request(),
                    executor,
                    cancellation=asyncio.Event(),
                )
            snapshot = await ledger.snapshot(
                "wsp_" + "1" * 32,
                "trn_" + "2" * 32,
                observed_at=clock(),
            )
        finally:
            await ledger.close()

        assert error.value.code is ProviderDispatchErrorCode.AMBIGUOUS
        assert executor.attempts == [1]
        assert snapshot.turn_settled_microusd == 50

    asyncio.run(scenario())


def test_pre_cancelled_dispatch_creates_no_reservation(tmp_path: Path) -> None:
    async def scenario() -> None:
        ledger = await SQLiteProviderCostLedger.open(
            private_database(tmp_path)
        )
        clock = Clock()
        coordinator = ProviderDispatchCoordinator(
            ledger,
            Delay(clock),
            clock=clock,
        )
        cancellation = asyncio.Event()
        cancellation.set()
        try:
            with pytest.raises(ProviderDispatchError) as error:
                await coordinator.dispatch(
                    dispatch_request(),
                    Executor([]),
                    cancellation=cancellation,
                )
            snapshot = await ledger.snapshot(
                "wsp_" + "1" * 32,
                "trn_" + "2" * 32,
                observed_at=clock(),
            )
        finally:
            await ledger.close()

        assert error.value.code is ProviderDispatchErrorCode.CANCELLED
        assert snapshot.turn_reserved_microusd == 0
        assert snapshot.active_reservations == 0

    asyncio.run(scenario())


def test_mismatched_failure_evidence_cannot_drive_retry(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        ledger = await SQLiteProviderCostLedger.open(
            private_database(tmp_path)
        )
        clock = Clock()
        executor = Executor([failure(request_sha256="9" * 64)])
        coordinator = ProviderDispatchCoordinator(
            ledger,
            Delay(clock),
            clock=clock,
        )
        try:
            with pytest.raises(ProviderDispatchError) as error:
                await coordinator.dispatch(
                    dispatch_request(),
                    executor,
                    cancellation=asyncio.Event(),
                )
        finally:
            await ledger.close()

        assert error.value.code is ProviderDispatchErrorCode.AMBIGUOUS
        assert executor.attempts == [1]

    asyncio.run(scenario())


def private_database(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    return tmp_path / "provider-dispatch.sqlite3"
