import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.harness.journal import (
    ProviderCostLedgerConflict,
    ProviderCostLedgerConflictCode,
    SQLiteProviderCostLedger,
)
from app.services.harness.protocol import (
    ProviderCostAdmissionCode,
    ProviderCostLimits,
    ProviderCostReleaseRequest,
    ProviderCostReservationRequest,
    ProviderCostReservationStatus,
    ProviderCostSettlementRequest,
)

NOW = datetime(2026, 7, 27, 16, 0, tzinfo=UTC)
WORKSPACE_ID = "wsp_" + "1" * 32
TURN_ID = "trn_" + "2" * 32


def reservation_request(
    number: int,
    *,
    estimated_cost_microusd: int,
    attempt: int = 1,
    turn_id: str = TURN_ID,
    limits: ProviderCostLimits | None = None,
) -> ProviderCostReservationRequest:
    return ProviderCostReservationRequest(
        reservation_id=f"pcs_{number:032x}",
        workspace_id=WORKSPACE_ID,
        turn_id=turn_id,
        request_id=f"req_{number:032x}",
        provider_request_sha256=f"{number:x}" * 64,
        attempt=attempt,
        estimated_cost_microusd=estimated_cost_microusd,
        limits=limits
        or ProviderCostLimits(
            max_call_microusd=100,
            max_turn_microusd=100,
            max_workspace_microusd=200,
        ),
        requested_at=NOW,
    )


def test_reservation_admission_is_atomic_idempotent_and_scoped(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        first_ledger = await SQLiteProviderCostLedger.open(path)
        second_ledger = await SQLiteProviderCostLedger.open(path)
        try:
            first = reservation_request(1, estimated_cost_microusd=60)
            replay_decisions = await asyncio.gather(
                first_ledger.reserve(first),
                second_ledger.reserve(first),
            )
            competing_turn = "trn_" + "8" * 32
            competing = await asyncio.gather(
                first_ledger.reserve(
                    reservation_request(
                        2,
                        estimated_cost_microusd=60,
                        turn_id=competing_turn,
                    )
                ),
                second_ledger.reserve(
                    reservation_request(
                        3,
                        estimated_cost_microusd=60,
                        turn_id=competing_turn,
                    )
                ),
            )
            snapshot = await first_ledger.snapshot(
                WORKSPACE_ID,
                TURN_ID,
                observed_at=NOW,
            )
        finally:
            await first_ledger.close()
            await second_ledger.close()

        assert all(decision.allowed for decision in replay_decisions)
        assert sorted(
            decision.already_exists for decision in replay_decisions
        ) == [False, True]
        assert sum(decision.allowed for decision in competing) == 1
        assert sum(
            decision.code is ProviderCostAdmissionCode.TURN_LIMIT
            for decision in competing
        ) == 1
        assert snapshot.turn_reserved_microusd == 60
        assert snapshot.workspace_reserved_microusd == 120
        assert snapshot.active_reservations == 1

    asyncio.run(scenario())


def test_call_workspace_and_duplicate_attempt_denials(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        ledger = await SQLiteProviderCostLedger.open(database_path(tmp_path))
        try:
            first = reservation_request(1, estimated_cost_microusd=60)
            await ledger.reserve(first)
            call_denial = await ledger.reserve(
                reservation_request(2, estimated_cost_microusd=101)
            )
            workspace_denial = await ledger.reserve(
                reservation_request(
                    3,
                    estimated_cost_microusd=150,
                    turn_id="trn_" + "4" * 32,
                    limits=ProviderCostLimits(
                        max_call_microusd=200,
                        max_turn_microusd=200,
                        max_workspace_microusd=200,
                    ),
                )
            )
            duplicate_values = first.model_dump(mode="python")
            duplicate_values["reservation_id"] = "pcs_" + "9" * 32
            duplicate = ProviderCostReservationRequest.model_validate(
                duplicate_values
            )
            with pytest.raises(ProviderCostLedgerConflict) as conflict:
                await ledger.reserve(duplicate)
        finally:
            await ledger.close()

        assert call_denial.code is ProviderCostAdmissionCode.CALL_LIMIT
        assert workspace_denial.code is ProviderCostAdmissionCode.WORKSPACE_LIMIT
        assert conflict.value.code is ProviderCostLedgerConflictCode.IDENTITY

    asyncio.run(scenario())


def test_settlement_release_and_restart_do_not_double_charge(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        ledger = await SQLiteProviderCostLedger.open(path)
        first = reservation_request(1, estimated_cost_microusd=60)
        second = reservation_request(2, estimated_cost_microusd=20)
        try:
            await ledger.reserve(first)
            settled_request = ProviderCostSettlementRequest(
                reservation_id=first.reservation_id,
                provider_request_sha256=first.provider_request_sha256,
                actual_cost_microusd=40,
                settled_at=NOW + timedelta(seconds=1),
            )
            settled = await ledger.settle(settled_request)
            replay = await ledger.settle(settled_request)
            await ledger.reserve(second)
            released = await ledger.release(
                ProviderCostReleaseRequest(
                    reservation_id=second.reservation_id,
                    provider_request_sha256=second.provider_request_sha256,
                    released_at=NOW + timedelta(seconds=2),
                )
            )
        finally:
            await ledger.close()

        reopened = await SQLiteProviderCostLedger.open(path)
        try:
            snapshot = await reopened.snapshot(
                WORKSPACE_ID,
                TURN_ID,
                observed_at=NOW + timedelta(seconds=3),
            )
            with pytest.raises(ProviderCostLedgerConflict) as terminal:
                await reopened.reserve(first)
        finally:
            await reopened.close()

        assert settled == replay
        assert settled.status is ProviderCostReservationStatus.SETTLED
        assert released.status is ProviderCostReservationStatus.RELEASED
        assert snapshot.workspace_reserved_microusd == 0
        assert snapshot.workspace_settled_microusd == 40
        assert snapshot.turn_settled_microusd == 40
        assert snapshot.active_reservations == 0
        assert terminal.value.code is ProviderCostLedgerConflictCode.STATE

    asyncio.run(scenario())


def test_reconciliation_overrun_fails_closed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        ledger = await SQLiteProviderCostLedger.open(database_path(tmp_path))
        request = reservation_request(1, estimated_cost_microusd=60)
        try:
            await ledger.reserve(request)
            with pytest.raises(ProviderCostLedgerConflict) as conflict:
                await ledger.settle(
                    ProviderCostSettlementRequest(
                        reservation_id=request.reservation_id,
                        provider_request_sha256=(
                            request.provider_request_sha256
                        ),
                        actual_cost_microusd=61,
                        settled_at=NOW + timedelta(seconds=1),
                    )
                )
            snapshot = await ledger.snapshot(
                WORKSPACE_ID,
                TURN_ID,
                observed_at=NOW + timedelta(seconds=2),
            )
        finally:
            await ledger.close()

        assert conflict.value.code is (
            ProviderCostLedgerConflictCode.RECONCILIATION
        )
        assert snapshot.turn_reserved_microusd == 60
        assert snapshot.turn_settled_microusd == 0

    asyncio.run(scenario())


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "provider-cost.sqlite3"
