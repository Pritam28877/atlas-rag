"""Bounded client reconnect, replay, and event-resync contracts."""

from __future__ import annotations

from collections import deque
from enum import StrEnum

from pydantic import Field

from app.services.harness.protocol import (
    CommandEnvelope,
    CommandKind,
    CommandReplayReceipt,
    MutatingCommand,
    RequestId,
    Sha256,
    StrictProtocolModel,
    SubscriptionCursorRecord,
    WorkspaceId,
    command_request_sha256,
)
from app.services.harness.runtime import ResyncRequired, SequencedEvent

MAXIMUM_PENDING_COMMANDS = 256


class ClientConnectionState(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RESYNC_REQUIRED = "resync_required"
    CLOSED = "closed"


class ReplayDecision(StrEnum):
    REPLAY = "replay"
    ALREADY_COMMITTED = "already_committed"
    HASH_CHANGED = "hash_changed"
    NOT_PENDING = "not_pending"
    QUERY_REFRESH = "query_refresh"


class PendingCommand(StrictProtocolModel):
    request_id: RequestId
    request_sha256: Sha256
    command_kind: CommandKind


class ClientRecoveryCheckpoint(StrictProtocolModel):
    workspace_id: WorkspaceId
    generation: int = Field(ge=1)
    acknowledged_sequence: int = Field(ge=0)
    delivered_sequence: int = Field(ge=0)
    pending: tuple[PendingCommand, ...] = Field(max_length=MAXIMUM_PENDING_COMMANDS)


class ClientRecoverySnapshot(StrictProtocolModel):
    state: ClientConnectionState
    generation: int = Field(ge=1)
    acknowledged_sequence: int = Field(ge=0)
    delivered_sequence: int = Field(ge=0)
    pending_count: int = Field(ge=0, le=MAXIMUM_PENDING_COMMANDS)


class ReplayPlan(StrictProtocolModel):
    decision: ReplayDecision
    request_sha256: Sha256
    command_kind: CommandKind


class ClientRecoveryCoordinator:
    """Keeps replay identity and event cursors bounded across reconnects."""

    def __init__(
        self,
        workspace_id: WorkspaceId,
        *,
        maximum_pending_commands: int = MAXIMUM_PENDING_COMMANDS,
    ) -> None:
        if not 1 <= maximum_pending_commands <= MAXIMUM_PENDING_COMMANDS:
            raise ValueError("pending command bound is outside the protocol limit")
        self._workspace_id = workspace_id
        self._maximum_pending_commands = maximum_pending_commands
        self._pending: dict[str, PendingCommand] = {}
        self._committed: set[str] = set()
        self._state = ClientConnectionState.CONNECTED
        self._generation = 1
        self._acknowledged_sequence = 0
        self._delivered_sequence = 0
        self._pending_order: deque[str] = deque()

    def register(self, envelope: CommandEnvelope) -> Sha256 | None:
        if not isinstance(envelope.command, MutatingCommand):
            return None
        request_hash = command_request_sha256(envelope)
        if request_hash not in self._pending:
            if len(self._pending) >= self._maximum_pending_commands:
                raise ValueError("pending command capacity is exhausted")
            self._pending[request_hash] = PendingCommand(
                request_id=envelope.request_id,
                request_sha256=request_hash,
                command_kind=CommandKind(envelope.command.kind),
            )
            self._pending_order.append(request_hash)
        return request_hash

    def record_receipt(self, receipt: CommandReplayReceipt) -> None:
        if receipt.workspace_id != self._workspace_id:
            raise ValueError("replay receipt belongs to another workspace")
        pending = self._pending.get(receipt.request_sha256)
        if pending is None:
            raise ValueError("replay receipt does not match pending command")
        if pending.command_kind is not receipt.command_kind:
            raise ValueError("replay receipt command kind does not match")
        self._committed.add(receipt.request_sha256)
        self._remove_pending(receipt.request_sha256)

    def plan_replay(self, envelope: CommandEnvelope) -> ReplayPlan:
        request_hash = command_request_sha256(envelope)
        command_kind = CommandKind(envelope.command.kind)
        if not isinstance(envelope.command, MutatingCommand):
            return ReplayPlan(
                decision=ReplayDecision.QUERY_REFRESH,
                request_sha256=request_hash,
                command_kind=command_kind,
            )
        pending = self._pending.get(request_hash)
        if request_hash in self._committed:
            decision = ReplayDecision.ALREADY_COMMITTED
        elif pending is None:
            same_request = any(
                item.request_id == envelope.request_id
                for item in self._pending.values()
            )
            decision = (
                ReplayDecision.HASH_CHANGED
                if same_request
                else ReplayDecision.NOT_PENDING
            )
        elif pending.command_kind is not command_kind:
            decision = ReplayDecision.HASH_CHANGED
        else:
            decision = ReplayDecision.REPLAY
        return ReplayPlan(
            decision=decision,
            request_sha256=request_hash,
            command_kind=command_kind,
        )

    def disconnect(self) -> None:
        if self._state is ClientConnectionState.CLOSED:
            raise RuntimeError("closed client cannot disconnect")
        self._state = ClientConnectionState.DISCONNECTED

    def restore(self, checkpoint: ClientRecoveryCheckpoint) -> None:
        if checkpoint.workspace_id != self._workspace_id:
            raise ValueError("checkpoint belongs to another workspace")
        if checkpoint.generation < self._generation:
            raise ValueError("checkpoint generation is stale")
        self._generation = checkpoint.generation
        self._acknowledged_sequence = checkpoint.acknowledged_sequence
        self._delivered_sequence = checkpoint.delivered_sequence
        self._pending.clear()
        self._pending_order.clear()
        for pending in checkpoint.pending:
            self._pending[pending.request_sha256] = pending
            self._pending_order.append(pending.request_sha256)
        self._state = ClientConnectionState.DISCONNECTED

    def reconnect(self, cursor: SubscriptionCursorRecord) -> None:
        if cursor.workspace_id != self._workspace_id:
            raise ValueError("subscription cursor belongs to another workspace")
        if cursor.generation <= self._generation:
            raise ValueError("subscription cursor generation is stale")
        if cursor.acknowledged_sequence > cursor.delivered_sequence:
            raise ValueError("subscription cursor window is invalid")
        self._generation = cursor.generation
        self._acknowledged_sequence = cursor.acknowledged_sequence
        self._delivered_sequence = cursor.delivered_sequence
        self._state = ClientConnectionState.CONNECTED

    def observe_delivery(self, delivery: SequencedEvent | ResyncRequired) -> None:
        if isinstance(delivery, ResyncRequired):
            self._state = ClientConnectionState.RESYNC_REQUIRED
            self._delivered_sequence = max(
                self._delivered_sequence,
                delivery.latest_observed_sequence,
            )
            return
        if delivery.journal_sequence <= self._delivered_sequence:
            raise ValueError("event sequence moved backwards")
        self._delivered_sequence = delivery.journal_sequence

    def acknowledge(self, through_sequence: int) -> None:
        if through_sequence < self._acknowledged_sequence:
            raise ValueError("acknowledgement moved backwards")
        if through_sequence > self._delivered_sequence:
            raise ValueError("cannot acknowledge an undelivered event")
        self._acknowledged_sequence = through_sequence

    def checkpoint(self) -> ClientRecoveryCheckpoint:
        return ClientRecoveryCheckpoint(
            workspace_id=self._workspace_id,
            generation=self._generation,
            acknowledged_sequence=self._acknowledged_sequence,
            delivered_sequence=self._delivered_sequence,
            pending=tuple(
                self._pending[request_hash] for request_hash in self._pending_order
            ),
        )

    def snapshot(self) -> ClientRecoverySnapshot:
        return ClientRecoverySnapshot(
            state=self._state,
            generation=self._generation,
            acknowledged_sequence=self._acknowledged_sequence,
            delivered_sequence=self._delivered_sequence,
            pending_count=len(self._pending),
        )

    def _remove_pending(self, request_hash: Sha256) -> None:
        self._pending.pop(request_hash, None)
        try:
            self._pending_order.remove(request_hash)
        except ValueError:
            return
