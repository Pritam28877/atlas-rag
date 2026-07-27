"""Durability-first operation lifecycle tests."""

import asyncio
from datetime import timedelta

import pytest

from app.services.harness.protocol import OperationState
from app.services.harness.tools.operation_lifecycle import (
    DurableOperationLifecycle,
    OperationDurabilityError,
)
from tests.harness.operations.fixtures import (
    NOW,
    WORKSPACE_ID,
    RecordingStore,
    fence,
    request,
)


def test_prepare_and_dispatch_persist_before_permit() -> None:
    async def scenario() -> None:
        store = RecordingStore()
        lifecycle = DurableOperationLifecycle(store)
        prepared = await lifecycle.prepare(request(), fence(), prepared_at=NOW)
        assert [entry[1].state for entry in store.records] == [
            OperationState.PREPARED
        ]

        permit = await lifecycle.dispatch(
            prepared,
            fence(),
            dispatched_at=NOW + timedelta(seconds=1),
        )

        assert [entry[1].state for entry in store.records] == [
            OperationState.PREPARED,
            OperationState.DISPATCHED,
        ]
        assert permit.dispatched_receipt.previous_receipt_sha256 == (
            permit.prepared_receipt.receipt_sha256
        )

    asyncio.run(scenario())


def test_dispatch_failure_returns_no_executor_permit() -> None:
    async def scenario() -> None:
        store = RecordingStore(fail_on_write=2)
        lifecycle = DurableOperationLifecycle(store)
        prepared = await lifecycle.prepare(request(), fence(), prepared_at=NOW)

        with pytest.raises(RuntimeError, match="durability failure"):
            await lifecycle.dispatch(
                prepared,
                fence(),
                dispatched_at=NOW + timedelta(seconds=1),
            )
        assert [entry[1].state for entry in store.records] == [
            OperationState.PREPARED
        ]

    asyncio.run(scenario())


def test_stale_or_mutated_fence_fails_before_persistence() -> None:
    async def scenario() -> None:
        store = RecordingStore()
        lifecycle = DurableOperationLifecycle(store)
        prepared = await lifecycle.prepare(request(), fence(), prepared_at=NOW)

        with pytest.raises(OperationDurabilityError, match="stale"):
            await lifecycle.dispatch(
                prepared,
                fence(fencing_token=8),
                dispatched_at=NOW + timedelta(seconds=1),
            )
        with pytest.raises(OperationDurabilityError, match="stale"):
            await lifecycle.dispatch(
                prepared,
                fence(),
                dispatched_at=NOW + timedelta(minutes=6),
            )
        assert len(store.records) == 1

    asyncio.run(scenario())


def test_typed_completion_is_durable() -> None:
    async def scenario() -> None:
        store = RecordingStore()
        lifecycle = DurableOperationLifecycle(store)
        prepared = await lifecycle.prepare(request(), fence(), prepared_at=NOW)
        permit = await lifecycle.dispatch(
            prepared,
            fence(),
            dispatched_at=NOW + timedelta(seconds=1),
        )
        completed = await lifecycle.complete(
            permit,
            workspace_id=WORKSPACE_ID,
            completed_at=NOW + timedelta(seconds=2),
            result_sha256="f" * 64,
        )

        assert completed.state is OperationState.COMPLETED
        assert store.records[-1][1] == completed

    asyncio.run(scenario())


def test_cancellation_before_dispatch_never_creates_a_permit() -> None:
    async def scenario() -> None:
        store = RecordingStore()
        lifecycle = DurableOperationLifecycle(store)
        prepared = await lifecycle.prepare(request(), fence(), prepared_at=NOW)
        cancelled = await lifecycle.cancel_prepared(
            prepared,
            workspace_id=WORKSPACE_ID,
            cancelled_at=NOW + timedelta(seconds=1),
            reason="Caller cancelled before dispatch.",
        )

        assert cancelled.state is OperationState.CANCELLED
        assert cancelled.dispatched_at is None
        assert [entry[1].state for entry in store.records] == [
            OperationState.PREPARED,
            OperationState.CANCELLED,
        ]

    asyncio.run(scenario())
