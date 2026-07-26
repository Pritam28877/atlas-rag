"""Crash after durable turn identity, then replay and resume in a new process."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path

from app.services.harness.journal import (
    AppendStatus,
    JournalReadRequest,
    SQLiteEventJournal,
    SQLiteJournalIntegrityVerifier,
    SQLiteSessionStore,
)
from app.services.harness.protocol import (
    InlinePayload,
    SubscriptionCursorRecord,
)
from app.services.harness.runtime import AuthenticatedCommandContext
from app.services.harness.sessions import ReplaySafeCommandDispatcher
from scripts.harness_session_resume_fixtures import (
    IDEMPOTENCY_KEY,
    NOW,
    PRINCIPAL_ID,
    SUBSCRIPTION_ID,
    TURN_ID,
    WORKSPACE_ID,
    append_request,
    command_context,
    payload,
)

CRASH_EXIT_CODE = 98


def increment_invocations(path: Path) -> int:
    current = int(path.read_text(encoding="utf-8")) if path.exists() else 0
    next_value = current + 1
    path.write_text(str(next_value), encoding="utf-8")
    return next_value


class JournalBackedTurnDispatcher:
    def __init__(
        self,
        journal: SQLiteEventJournal,
        invocation_path: Path,
        *,
        crash_after_append: bool,
    ) -> None:
        self._journal = journal
        self._invocation_path = invocation_path
        self._crash_after_append = crash_after_append
        self.last_append_status: AppendStatus | None = None

    async def dispatch(
        self,
        context: AuthenticatedCommandContext,
        cancellation_event: asyncio.Event,
    ) -> InlinePayload:
        if cancellation_event.is_set():
            raise asyncio.CancelledError
        await asyncio.to_thread(increment_invocations, self._invocation_path)
        append_result = await self._journal.append(append_request(context))
        self.last_append_status = append_result.status
        if self._crash_after_append:
            os._exit(CRASH_EXIT_CODE)
        return payload(TURN_ID)


async def seed_cursor(store: SQLiteSessionStore) -> None:
    await store.save_subscription_cursor(
        SubscriptionCursorRecord(
            workspace_id=WORKSPACE_ID,
            principal_id=PRINCIPAL_ID,
            subscription_id=SUBSCRIPTION_ID,
            generation=1,
            acknowledged_sequence=3,
            delivered_sequence=5,
            updated_at=NOW,
            expires_at=NOW + timedelta(days=7),
        )
    )


async def run_crash(
    database_path: Path,
    invocation_path: Path,
) -> None:
    journal = await SQLiteEventJournal.open(database_path, clock=lambda: NOW)
    store = await SQLiteSessionStore.open(database_path)
    await seed_cursor(store)
    inner = JournalBackedTurnDispatcher(
        journal,
        invocation_path,
        crash_after_append=True,
    )
    dispatcher = ReplaySafeCommandDispatcher(store, inner, clock=lambda: NOW)
    await dispatcher.dispatch(command_context(1), asyncio.Event())
    raise RuntimeError("crash fault point did not terminate process")


async def run_reconnect(
    database_path: Path,
    invocation_path: Path,
) -> dict[str, object]:
    journal = await SQLiteEventJournal.open(database_path, clock=lambda: NOW)
    store = await SQLiteSessionStore.open(database_path)
    inner = JournalBackedTurnDispatcher(
        journal,
        invocation_path,
        crash_after_append=False,
    )
    dispatcher = ReplaySafeCommandDispatcher(store, inner, clock=lambda: NOW)
    first_result = await dispatcher.dispatch(command_context(2), asyncio.Event())
    duplicate_result = await dispatcher.dispatch(
        command_context(3),
        asyncio.Event(),
    )
    page = await journal.read_aggregate(
        JournalReadRequest(
            workspace_id=WORKSPACE_ID,
            aggregate_id=TURN_ID,
            after_sequence=0,
        )
    )
    cursor = await store.load_subscription_cursor(
        WORKSPACE_ID,
        PRINCIPAL_ID,
        SUBSCRIPTION_ID,
        observed_at=NOW + timedelta(hours=1),
    )
    receipt = await store.load_command_receipt(
        WORKSPACE_ID,
        PRINCIPAL_ID,
        IDEMPOTENCY_KEY,
    )
    verifier = await SQLiteJournalIntegrityVerifier.open(database_path)
    verification = await verifier.verify()
    await verifier.close()
    await store.close()
    await journal.close()
    if cursor is None or receipt is None:
        raise RuntimeError("reconnect evidence is incomplete")
    if not isinstance(first_result, InlinePayload):
        raise RuntimeError("reconnect result must be inline")
    return {
        "acknowledged_sequence": cursor.acknowledged_sequence,
        "append_status": (
            inner.last_append_status.value
            if inner.last_append_status is not None
            else None
        ),
        "delivered_sequence": cursor.delivered_sequence,
        "duplicate_result_matches": duplicate_result == first_result,
        "event_count": len(page.events),
        "inner_invocations": int(invocation_path.read_text(encoding="utf-8")),
        "journal_verified": verification.complete,
        "receipt_result_matches": receipt.result == first_result,
        "result_text": first_result.text,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-path", required=True, type=Path)
    parser.add_argument("--invocation-path", required=True, type=Path)
    parser.add_argument(
        "--mode",
        required=True,
        choices=("crash", "reconnect"),
    )
    arguments = parser.parse_args()
    if arguments.mode == "crash":
        asyncio.run(
            run_crash(arguments.database_path, arguments.invocation_path)
        )
        return 1
    report = asyncio.run(
        run_reconnect(arguments.database_path, arguments.invocation_path)
    )
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
