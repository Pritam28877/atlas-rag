"""Restart, concurrency, ownership, and immutability approval-store tests."""

import asyncio
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.services.harness.journal import (
    ApprovalStoreConflict,
    ApprovalStoreConflictCode,
    SQLiteApprovalStore,
)
from app.services.harness.policy import respond_to_approval
from app.services.harness.protocol.approvals import DurableApprovalState
from tests.harness.policy.approval_fixtures import (
    APPROVAL_ID,
    GRANT_ID,
    PRINCIPAL_ID,
    WORKSPACE_ID,
    pending,
)
from tests.harness.policy.fixtures import NOW


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def approve(record):
    return respond_to_approval(
        record,
        authorization_sha256=record.binding.authorization_sha256,
        decision=DurableApprovalState.APPROVED,
        principal_id=PRINCIPAL_ID,
        grant_id=GRANT_ID,
        policy_version=record.binding.policy_version,
        decided_at=NOW + timedelta(minutes=1),
        reason="Approved exact request.",
    )


def test_pending_request_survives_disconnect_and_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        requested, receipt = pending()
        store = await SQLiteApprovalStore.open(path)
        assert await store.save_request(requested, receipt) == requested
        await store.close()

        reopened = await SQLiteApprovalStore.open(path)
        restored = await reopened.load(
            WORKSPACE_ID,
            PRINCIPAL_ID,
            APPROVAL_ID,
        )
        receipts = await reopened.receipts(
            WORKSPACE_ID,
            PRINCIPAL_ID,
            APPROVAL_ID,
        )
        await reopened.close()

        assert restored == requested
        assert receipts == (receipt,)

    asyncio.run(scenario())


def test_response_and_receipt_chain_survive_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        requested, request_receipt = pending()
        approved, approval_receipt = approve(requested)
        store = await SQLiteApprovalStore.open(path)
        await store.save_request(requested, request_receipt)
        await store.transition(requested, approved, approval_receipt)
        await store.close()

        reopened = await SQLiteApprovalStore.open(path)
        restored = await reopened.load(
            WORKSPACE_ID,
            PRINCIPAL_ID,
            APPROVAL_ID,
        )
        receipts = await reopened.receipts(
            WORKSPACE_ID,
            PRINCIPAL_ID,
            APPROVAL_ID,
        )
        await reopened.close()

        assert restored == approved
        assert receipts == (request_receipt, approval_receipt)

    asyncio.run(scenario())


def test_concurrent_responses_cannot_overwrite_first_transition(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        requested, request_receipt = pending()
        approved, approval_receipt = approve(requested)
        first = await SQLiteApprovalStore.open(path)
        second = await SQLiteApprovalStore.open(path)
        await first.save_request(requested, request_receipt)

        outcomes = await asyncio.gather(
            first.transition(requested, approved, approval_receipt),
            second.transition(requested, approved, approval_receipt),
            return_exceptions=True,
        )
        await first.close()
        await second.close()

        successful = [
            outcome
            for outcome in outcomes
            if not isinstance(outcome, BaseException)
        ]
        conflicts = [
            outcome
            for outcome in outcomes
            if isinstance(outcome, ApprovalStoreConflict)
        ]
        assert successful == [approved]
        assert len(conflicts) == 1
        assert conflicts[0].code is (
            ApprovalStoreConflictCode.CONCURRENT_TRANSITION
        )

    asyncio.run(scenario())


def test_cross_principal_load_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        requested, receipt = pending()
        store = await SQLiteApprovalStore.open(database_path(tmp_path))
        await store.save_request(requested, receipt)
        with pytest.raises(ApprovalStoreConflict) as conflict:
            await store.load(
                WORKSPACE_ID,
                "prn_" + "a" * 32,
                APPROVAL_ID,
            )
        await store.close()
        assert conflict.value.code is ApprovalStoreConflictCode.OWNER

    asyncio.run(scenario())


def test_sqlite_guards_record_and_receipt_evidence(tmp_path: Path) -> None:
    async def initialize(path: Path) -> None:
        requested, receipt = pending()
        store = await SQLiteApprovalStore.open(path)
        await store.save_request(requested, receipt)
        await store.close()

    path = database_path(tmp_path)
    asyncio.run(initialize(path))
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="transition"):
            connection.execute(
                """
                UPDATE harness_approval_records
                SET principal_id = ?, generation = 2, approval_state = 'approved'
                WHERE workspace_id = ? AND approval_id = ?
                """,
                ("prn_" + "b" * 32, WORKSPACE_ID, APPROVAL_ID),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                DELETE FROM harness_approval_receipts
                WHERE workspace_id = ? AND approval_id = ?
                """,
                (WORKSPACE_ID, APPROVAL_ID),
            )
    finally:
        connection.close()


def test_restored_record_rejects_json_column_mismatch(tmp_path: Path) -> None:
    async def initialize(path: Path) -> None:
        requested, receipt = pending()
        store = await SQLiteApprovalStore.open(path)
        await store.save_request(requested, receipt)
        await store.close()

    async def load_tampered(path: Path) -> None:
        store = await SQLiteApprovalStore.open(path)
        with pytest.raises(ApprovalStoreConflict) as conflict:
            await store.load(WORKSPACE_ID, PRINCIPAL_ID, APPROVAL_ID)
        await store.close()
        assert conflict.value.code is ApprovalStoreConflictCode.IDENTITY

    path = database_path(tmp_path)
    asyncio.run(initialize(path))
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            UPDATE harness_approval_records
            SET generation = 2, approval_state = 'approved',
                updated_at = '2026-07-27T00:01:00+00:00'
            WHERE workspace_id = ? AND approval_id = ?
            """,
            (WORKSPACE_ID, APPROVAL_ID),
        )
        connection.commit()
    finally:
        connection.close()
    asyncio.run(load_tampered(path))
