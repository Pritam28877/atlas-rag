import asyncio
import hashlib
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.harness.journal import (
    JournalStorageError,
    SessionStoreConflict,
    SessionStoreConflictCode,
    SQLiteSessionStore,
)
from app.services.harness.protocol import (
    CommandEnvelope,
    CommandKind,
    CommandReplayReceipt,
    CommandResponseKind,
    DataClassification,
    ExecutionBudget,
    InlinePayload,
    SubscriptionCursorRecord,
    TurnStartCommand,
    command_request_sha256,
    command_result_sha256,
)

NOW = datetime(2026, 7, 27, 13, 0, tzinfo=UTC)
WORKSPACE_ID = "wsp_" + "1" * 32
OTHER_WORKSPACE_ID = "wsp_" + "2" * 32
PRINCIPAL_ID = "prn_" + "3" * 32
OTHER_PRINCIPAL_ID = "prn_" + "4" * 32


def identifier(prefix: str, number: int) -> str:
    return f"{prefix}_{number:032x}"


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def payload(text: str) -> InlinePayload:
    encoded = text.encode()
    return InlinePayload(
        text=text,
        size_bytes=len(encoded),
        content_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def receipt(
    number: int,
    *,
    workspace_id: str = WORKSPACE_ID,
    principal_id: str = PRINCIPAL_ID,
    request_sha256: str | None = None,
    result_text: str | None = None,
    committed_at: datetime = NOW,
) -> CommandReplayReceipt:
    result = payload(result_text or f"turn-result-{number}")
    return CommandReplayReceipt(
        workspace_id=workspace_id,
        principal_id=principal_id,
        idempotency_key=f"turn-command-{number:04d}",
        command_kind=CommandKind.TURN_START,
        request_sha256=request_sha256 or f"{number:064x}",
        response_kind=CommandResponseKind.TURN_STATUS,
        result=result,
        result_sha256=command_result_sha256(result),
        committed_at=committed_at,
    )


def cursor(
    number: int,
    *,
    generation: int = 1,
    acknowledged_sequence: int = 0,
    delivered_sequence: int = 0,
    principal_id: str = PRINCIPAL_ID,
    updated_at: datetime = NOW,
    expires_at: datetime | None = None,
) -> SubscriptionCursorRecord:
    return SubscriptionCursorRecord(
        workspace_id=WORKSPACE_ID,
        principal_id=principal_id,
        subscription_id=identifier("sub", number),
        generation=generation,
        acknowledged_sequence=acknowledged_sequence,
        delivered_sequence=delivered_sequence,
        updated_at=updated_at,
        expires_at=expires_at or updated_at + timedelta(days=7),
    )


def turn_envelope(
    *,
    request_number: int,
    client_number: int,
    text: str,
) -> CommandEnvelope:
    return CommandEnvelope(
        schema_version="1.2",
        request_id=identifier("req", request_number),
        client_id=identifier("cli", client_number),
        workspace_id=WORKSPACE_ID,
        expected_sequence=0,
        command=TurnStartCommand(
            idempotency_key="turn-command-hash-0001",
            thread_id=identifier("thr", 1),
            requested_agent="coding-agent",
            budget=ExecutionBudget(
                max_steps=8,
                max_tool_calls=4,
                max_input_tokens=16_000,
                max_output_tokens=4_000,
                max_tool_output_bytes=4_096,
                max_duration_ms=30_000,
                max_cost_microusd=100_000,
            ),
            classification=DataClassification.INTERNAL,
            initial_payload=payload(text),
        ),
    )


def test_command_hash_ignores_reconnect_transport_identity() -> None:
    first = turn_envelope(request_number=1, client_number=1, text="same request")
    reconnected = turn_envelope(
        request_number=2,
        client_number=2,
        text="same request",
    )
    changed = turn_envelope(
        request_number=3,
        client_number=2,
        text="different request",
    )

    assert command_request_sha256(first) == command_request_sha256(reconnected)
    assert command_request_sha256(first) != command_request_sha256(changed)


def test_command_receipt_replays_original_after_reopen(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        original = receipt(1)
        store = await SQLiteSessionStore.open(path)
        assert await store.save_command_receipt(original) == original

        duplicate_result = receipt(
            1,
            result_text="a duplicate execution produced another response",
            committed_at=NOW + timedelta(seconds=1),
        )
        replay = await store.save_command_receipt(duplicate_result)
        await store.close()

        reopened = await SQLiteSessionStore.open(path)
        restored = await reopened.load_command_receipt(
            WORKSPACE_ID,
            PRINCIPAL_ID,
            original.idempotency_key,
        )
        await reopened.close()

        assert replay == original
        assert restored == original

    asyncio.run(scenario())


def test_concurrent_command_receipts_converge_on_one_result(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        first_store = await SQLiteSessionStore.open(path)
        second_store = await SQLiteSessionStore.open(path)
        first_candidate = receipt(1, result_text="first candidate")
        second_candidate = receipt(
            1,
            result_text="second candidate",
            committed_at=NOW + timedelta(microseconds=1),
        )

        first_result, second_result = await asyncio.gather(
            first_store.save_command_receipt(first_candidate),
            second_store.save_command_receipt(second_candidate),
        )
        restored = await first_store.load_command_receipt(
            WORKSPACE_ID,
            PRINCIPAL_ID,
            first_candidate.idempotency_key,
        )
        await first_store.close()
        await second_store.close()

        assert first_result == second_result
        assert restored == first_result
        assert restored in {first_candidate, second_candidate}

    asyncio.run(scenario())


def test_command_receipt_rejects_mismatch_owner_and_capacity(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = await SQLiteSessionStore.open(
            database_path(tmp_path),
            maximum_command_receipts_per_workspace=1,
        )
        original = receipt(1)
        await store.save_command_receipt(original)

        with pytest.raises(SessionStoreConflict) as mismatch:
            await store.save_command_receipt(
                receipt(1, request_sha256="f" * 64)
            )
        with pytest.raises(SessionStoreConflict) as owner:
            await store.load_command_receipt(
                WORKSPACE_ID,
                OTHER_PRINCIPAL_ID,
                original.idempotency_key,
            )
        with pytest.raises(SessionStoreConflict) as capacity:
            await store.save_command_receipt(receipt(2))

        other_workspace = receipt(2, workspace_id=OTHER_WORKSPACE_ID)
        assert await store.save_command_receipt(other_workspace) == other_workspace
        await store.close()

        assert mismatch.value.code is (
            SessionStoreConflictCode.IDEMPOTENCY_MISMATCH
        )
        assert owner.value.code is SessionStoreConflictCode.OWNER_MISMATCH
        assert capacity.value.code is SessionStoreConflictCode.CAPACITY

    asyncio.run(scenario())


def test_corrupt_command_result_fails_closed(tmp_path: Path) -> None:
    async def initialize(path: Path) -> None:
        store = await SQLiteSessionStore.open(path)
        await store.close()

    path = database_path(tmp_path)
    asyncio.run(initialize(path))
    invalid_result_json = payload("corrupt").model_dump_json()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            INSERT INTO harness_command_receipts (
                workspace_id, principal_id, idempotency_key, command_kind,
                request_sha256, response_kind, result_json, result_sha256,
                committed_at
            ) VALUES (?, ?, ?, 'turn.start', ?, 'turn_status', ?, ?, ?)
            """,
            (
                WORKSPACE_ID,
                PRINCIPAL_ID,
                "turn-command-corrupt",
                "a" * 64,
                invalid_result_json,
                "b" * 64,
                NOW.isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    async def load() -> None:
        store = await SQLiteSessionStore.open(path)
        with pytest.raises(JournalStorageError, match="receipt"):
            await store.load_command_receipt(
                WORKSPACE_ID,
                PRINCIPAL_ID,
                "turn-command-corrupt",
            )
        await store.close()

    asyncio.run(load())


def test_subscription_cursor_resumes_monotonically_after_reopen(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        store = await SQLiteSessionStore.open(path)
        initial = cursor(1, delivered_sequence=3)
        assert await store.save_subscription_cursor(initial) == initial
        assert await store.save_subscription_cursor(initial) == initial

        advanced = cursor(
            1,
            generation=2,
            acknowledged_sequence=3,
            delivered_sequence=5,
            updated_at=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(days=8),
        )
        assert await store.save_subscription_cursor(advanced) == advanced
        await store.close()

        reopened = await SQLiteSessionStore.open(path)
        restored = await reopened.load_subscription_cursor(
            WORKSPACE_ID,
            PRINCIPAL_ID,
            advanced.subscription_id,
            observed_at=NOW + timedelta(days=1),
        )
        await reopened.close()

        assert restored == advanced

    asyncio.run(scenario())


def test_subscription_cursor_rejects_stale_or_foreign_updates(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = await SQLiteSessionStore.open(database_path(tmp_path))
        initial = cursor(1, acknowledged_sequence=2, delivered_sequence=4)
        await store.save_subscription_cursor(initial)

        stale = cursor(
            1,
            generation=2,
            acknowledged_sequence=1,
            delivered_sequence=4,
            updated_at=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(days=8),
        )
        with pytest.raises(SessionStoreConflict) as stale_error:
            await store.save_subscription_cursor(stale)
        foreign = initial.model_copy(
            update={"principal_id": OTHER_PRINCIPAL_ID}
        )
        with pytest.raises(SessionStoreConflict) as owner_error:
            await store.save_subscription_cursor(foreign)
        await store.close()

        assert stale_error.value.code is SessionStoreConflictCode.CURSOR_CONFLICT
        assert owner_error.value.code is SessionStoreConflictCode.OWNER_MISMATCH

    asyncio.run(scenario())


def test_expired_cursors_are_bounded_and_purged(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await SQLiteSessionStore.open(
            database_path(tmp_path),
            maximum_subscription_cursors_per_workspace=1,
        )
        expiring = cursor(
            1,
            expires_at=NOW + timedelta(seconds=1),
        )
        await store.save_subscription_cursor(expiring)
        missing = await store.load_subscription_cursor(
            WORKSPACE_ID,
            PRINCIPAL_ID,
            expiring.subscription_id,
            observed_at=NOW + timedelta(seconds=1),
        )

        replacement = cursor(
            2,
            updated_at=NOW + timedelta(seconds=2),
        )
        await store.save_subscription_cursor(replacement)
        removed = await store.purge_expired_subscription_cursors(
            expired_at_or_before=NOW + timedelta(days=8),
            maximum_records=1,
        )
        after_purge = await store.load_subscription_cursor(
            WORKSPACE_ID,
            PRINCIPAL_ID,
            replacement.subscription_id,
            observed_at=NOW + timedelta(seconds=3),
        )
        await store.close()

        assert missing is None
        assert removed == 1
        assert after_purge is None

    asyncio.run(scenario())
