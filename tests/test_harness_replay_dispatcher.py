import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.harness.journal import SQLiteSessionStore
from app.services.harness.protocol import (
    AuthenticationMethod,
    CommandEnvelope,
    CommandKind,
    DataClassification,
    ExecutionBudget,
    GrantRecord,
    GrantState,
    InlinePayload,
    PrincipalRecord,
    TurnStartCommand,
    WorkspaceRecord,
    command_contract,
)
from app.services.harness.runtime import AuthenticatedCommandContext
from app.services.harness.sessions import (
    ReplayDispatcherError,
    ReplayDispatcherErrorCode,
    ReplaySafeCommandDispatcher,
)

NOW = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
WORKSPACE_ID = "wsp_" + "1" * 32
PRINCIPAL_ID = "prn_" + "2" * 32
POLICY_VERSION = "pol_" + "3" * 64


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


def principal() -> PrincipalRecord:
    return PrincipalRecord(
        principal_id=PRINCIPAL_ID,
        authentication_method=AuthenticationMethod.PEER_CREDENTIALS,
        issuer="atlas-local-peer",
        subject_sha256="4" * 64,
        session_binding_sha256="5" * 64,
        authenticated_at=NOW - timedelta(minutes=1),
    )


def workspace() -> WorkspaceRecord:
    return WorkspaceRecord(
        workspace_id=WORKSPACE_ID,
        tenant_id=identifier("ten", 1),
        owner_principal_id=PRINCIPAL_ID,
        root_uri="file:///srv/workspaces/atlas",
        repository_fingerprint_sha256="6" * 64,
        policy_version=POLICY_VERSION,
        created_at=NOW - timedelta(days=1),
    )


def context(
    number: int,
    *,
    idempotency_key: str = "replay-command-0001",
    text: str = "same semantic request",
) -> AuthenticatedCommandContext:
    command = TurnStartCommand(
        idempotency_key=idempotency_key,
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
    )
    envelope = CommandEnvelope(
        schema_version="1.2",
        request_id=identifier("req", number),
        client_id=identifier("cli", number),
        workspace_id=WORKSPACE_ID,
        expected_sequence=0,
        command=command,
    )
    grant = GrantRecord(
        grant_id=identifier("grt", 1),
        principal_id=PRINCIPAL_ID,
        workspace_id=WORKSPACE_ID,
        roles=("developer",),
        capabilities=("turn.start",),
        policy_version=POLICY_VERSION,
        state=GrantState.ACTIVE,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    return AuthenticatedCommandContext(
        principal=principal(),
        grant=grant,
        workspace=workspace(),
        contract=command_contract(CommandKind.TURN_START),
        envelope=envelope,
        authorized_at=NOW,
    )


class GateDispatcher:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def dispatch(
        self,
        context: AuthenticatedCommandContext,
        cancellation_event: asyncio.Event,
    ) -> InlinePayload:
        _ = context, cancellation_event
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return payload("durable result")


class ConcurrentDispatcher:
    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.calls = 0
        self.active = 0
        self.maximum_active = 0
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()

    async def dispatch(
        self,
        context: AuthenticatedCommandContext,
        cancellation_event: asyncio.Event,
    ) -> InlinePayload:
        _ = cancellation_event
        self.calls += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        if self.active == self.expected:
            self.all_started.set()
        try:
            await self.release.wait()
            return payload(context.envelope.command.idempotency_key)
        finally:
            self.active -= 1


class FailOnceDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(
        self,
        context: AuthenticatedCommandContext,
        cancellation_event: asyncio.Event,
    ) -> InlinePayload:
        _ = context, cancellation_event
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("injected command failure")
        return payload("recovered result")


def test_concurrent_duplicate_executes_once_and_replays(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await SQLiteSessionStore.open(database_path(tmp_path))
        inner = GateDispatcher()
        dispatcher = ReplaySafeCommandDispatcher(
            store,
            inner,
            clock=lambda: NOW,
        )
        first_task = asyncio.create_task(
            dispatcher.dispatch(context(1), asyncio.Event())
        )
        await inner.started.wait()
        duplicate_task = asyncio.create_task(
            dispatcher.dispatch(context(2), asyncio.Event())
        )
        await asyncio.sleep(0)
        inner.release.set()
        first, duplicate = await asyncio.gather(first_task, duplicate_task)
        active_keys = await dispatcher.active_replay_keys()
        await store.close()

        assert first == duplicate == payload("durable result")
        assert inner.calls == 1
        assert active_keys == 0

    asyncio.run(scenario())


def test_reused_key_with_different_request_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await SQLiteSessionStore.open(database_path(tmp_path))
        inner = GateDispatcher()
        inner.release.set()
        dispatcher = ReplaySafeCommandDispatcher(
            store,
            inner,
            clock=lambda: NOW,
        )
        await dispatcher.dispatch(context(1), asyncio.Event())

        with pytest.raises(ReplayDispatcherError) as mismatch:
            await dispatcher.dispatch(
                context(2, text="changed semantic request"),
                asyncio.Event(),
            )
        await store.close()

        assert mismatch.value.code is (
            ReplayDispatcherErrorCode.IDEMPOTENCY_MISMATCH
        )
        assert inner.calls == 1

    asyncio.run(scenario())


def test_independent_keys_run_concurrently_and_capacity_is_bounded(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = await SQLiteSessionStore.open(database_path(tmp_path))
        inner = ConcurrentDispatcher(expected=2)
        dispatcher = ReplaySafeCommandDispatcher(
            store,
            inner,
            maximum_active_replay_keys=2,
            clock=lambda: NOW,
        )
        first_task = asyncio.create_task(
            dispatcher.dispatch(
                context(1, idempotency_key="replay-command-0001"),
                asyncio.Event(),
            )
        )
        second_task = asyncio.create_task(
            dispatcher.dispatch(
                context(2, idempotency_key="replay-command-0002"),
                asyncio.Event(),
            )
        )
        await asyncio.wait_for(inner.all_started.wait(), timeout=1)
        with pytest.raises(ReplayDispatcherError) as capacity:
            await dispatcher.dispatch(
                context(3, idempotency_key="replay-command-0003"),
                asyncio.Event(),
            )
        inner.release.set()
        await asyncio.gather(first_task, second_task)
        active_keys = await dispatcher.active_replay_keys()
        await store.close()

        assert capacity.value.code is ReplayDispatcherErrorCode.CAPACITY
        assert inner.calls == 2
        assert inner.maximum_active == 2
        assert active_keys == 0

    asyncio.run(scenario())


def test_failed_dispatch_releases_replay_capacity(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await SQLiteSessionStore.open(database_path(tmp_path))
        inner = FailOnceDispatcher()
        dispatcher = ReplaySafeCommandDispatcher(
            store,
            inner,
            maximum_active_replay_keys=1,
            clock=lambda: NOW,
        )

        with pytest.raises(RuntimeError, match="injected"):
            await dispatcher.dispatch(context(1), asyncio.Event())
        assert await dispatcher.active_replay_keys() == 0
        recovered = await dispatcher.dispatch(context(2), asyncio.Event())
        assert await dispatcher.active_replay_keys() == 0
        await store.close()

        assert recovered == payload("recovered result")
        assert inner.calls == 2

    asyncio.run(scenario())
