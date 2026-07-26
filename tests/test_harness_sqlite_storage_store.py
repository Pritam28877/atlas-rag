import asyncio
import os
from datetime import timedelta
from pathlib import Path

from app.services.harness.artifacts import (
    BlobReservationStatus,
    RetentionPolicy,
    StorageSealReason,
)
from app.services.harness.journal import SQLiteStorageStore
from scripts.probe_harness_sqlite_kill import NOW

WORKSPACE_ID = "wsp_" + "0" * 32
MEBIBYTE = 1024 * 1024


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def test_reservation_commit_and_idempotent_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        policy = RetentionPolicy(
            blob_quota_bytes=2 * MEBIBYTE,
            disk_reserve_bytes=MEBIBYTE,
        )
        store = await SQLiteStorageStore.open(path)
        decision = await store.reserve(
            policy,
            WORKSPACE_ID,
            "a" * 64,
            MEBIBYTE,
            available_filesystem_bytes=2 * MEBIBYTE,
            reserved_at=NOW,
        )
        duplicate = await store.reserve(
            policy,
            WORKSPACE_ID,
            "a" * 64,
            MEBIBYTE,
            available_filesystem_bytes=2 * MEBIBYTE,
            reserved_at=NOW,
        )
        assert decision.reservation is not None
        committed = await store.commit(
            decision.reservation,
            committed_at=NOW + timedelta(seconds=1),
        )
        await store.close()

        reopened = await SQLiteStorageStore.open(path)
        replay = await reopened.reserve(
            policy,
            WORKSPACE_ID,
            "a" * 64,
            MEBIBYTE,
            available_filesystem_bytes=2 * MEBIBYTE,
            reserved_at=NOW + timedelta(seconds=2),
        )
        state = await reopened.state(
            WORKSPACE_ID,
            available_filesystem_bytes=2 * MEBIBYTE,
        )
        await reopened.close()

        assert duplicate.reservation == decision.reservation
        assert committed.content_sha256 == "a" * 64
        assert replay.already_committed
        assert replay.reservation is not None
        assert replay.reservation.status is BlobReservationStatus.COMMITTED
        assert state.used_blob_bytes == MEBIBYTE
        assert state.reserved_blob_bytes == 0

    asyncio.run(scenario())


def test_concurrent_reservations_cannot_oversubscribe_quota(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        policy = RetentionPolicy(
            blob_quota_bytes=MEBIBYTE,
            disk_reserve_bytes=MEBIBYTE,
        )
        first_store = await SQLiteStorageStore.open(path)
        second_store = await SQLiteStorageStore.open(path)
        decisions = await asyncio.gather(
            first_store.reserve(
                policy,
                WORKSPACE_ID,
                "b" * 64,
                700 * 1024,
                available_filesystem_bytes=4 * MEBIBYTE,
                reserved_at=NOW,
            ),
            second_store.reserve(
                policy,
                WORKSPACE_ID,
                "c" * 64,
                700 * 1024,
                available_filesystem_bytes=4 * MEBIBYTE,
                reserved_at=NOW,
            ),
        )
        state = await first_store.state(
            WORKSPACE_ID,
            available_filesystem_bytes=4 * MEBIBYTE,
        )
        await first_store.close()
        await second_store.close()

        allowed = tuple(item for item in decisions if item.admission.allowed)
        denied = tuple(item for item in decisions if not item.admission.allowed)
        assert len(allowed) == 1
        assert len(denied) == 1
        assert denied[0].admission.reason is StorageSealReason.WORKSPACE_QUOTA
        assert state.reserved_blob_bytes == 700 * 1024
        assert state.sealed

    asyncio.run(scenario())


def test_release_restores_capacity_and_disk_boundary_seals(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        policy = RetentionPolicy(
            blob_quota_bytes=2 * MEBIBYTE,
            disk_reserve_bytes=MEBIBYTE,
        )
        store = await SQLiteStorageStore.open(path)
        reserved = await store.reserve(
            policy,
            WORKSPACE_ID,
            "d" * 64,
            MEBIBYTE,
            available_filesystem_bytes=2 * MEBIBYTE,
            reserved_at=NOW,
        )
        assert reserved.reservation is not None
        released = await store.release(
            reserved.reservation,
            released_at=NOW + timedelta(seconds=1),
        )
        denied = await store.reserve(
            policy,
            WORKSPACE_ID,
            "e" * 64,
            MEBIBYTE,
            available_filesystem_bytes=2 * MEBIBYTE - 1,
            reserved_at=NOW + timedelta(seconds=2),
        )
        repeated = await store.reserve(
            policy,
            WORKSPACE_ID,
            "f" * 64,
            1,
            available_filesystem_bytes=4 * MEBIBYTE,
            reserved_at=NOW + timedelta(seconds=3),
        )
        await store.close()

        assert released.status is BlobReservationStatus.RELEASED
        assert denied.admission.reason is StorageSealReason.DISK_RESERVE
        assert repeated.admission.reason is StorageSealReason.ALREADY_SEALED

    asyncio.run(scenario())
