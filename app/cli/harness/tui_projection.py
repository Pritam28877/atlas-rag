"""Bounded, keyboard-driven TUI projection with secret-free rendering."""

from __future__ import annotations

from collections import deque
from enum import StrEnum
from typing import TypeVar

from pydantic import Field

from app.services.harness.protocol import (
    ApprovalRecord,
    StrictProtocolModel,
    TaskNodeRecord,
)
from app.services.harness.runtime import ResyncRequired, SequencedEvent

MAXIMUM_TUI_EVENTS = 512
MAXIMUM_TUI_TASKS = 256
MAXIMUM_TUI_APPROVALS = 256
MAXIMUM_RENDER_LINES = 80
MAXIMUM_RENDER_LINE_BYTES = 160
_Row = TypeVar("_Row")


class TuiFocus(StrEnum):
    EVENTS = "events"
    TASKS = "tasks"
    APPROVALS = "approvals"


class TuiKey(StrEnum):
    UP = "up"
    DOWN = "down"
    NEXT_PANEL = "next_panel"
    PREVIOUS_PANEL = "previous_panel"
    ENTER = "enter"
    APPROVE = "approve"
    DENY = "deny"
    CANCEL = "cancel"
    RESYNC = "resync"


class TuiActionKind(StrEnum):
    NONE = "none"
    INSPECT = "inspect"
    APPROVE = "approve"
    DENY = "deny"
    CANCEL = "cancel"
    RESYNC = "resync"


class TuiEventRow(StrictProtocolModel):
    journal_sequence: int = Field(ge=1)
    event_id: str
    event_type: str


class TuiTaskRow(StrictProtocolModel):
    task_id: str
    state: str
    depth: int = Field(ge=0)


class TuiApprovalRow(StrictProtocolModel):
    approval_id: str
    state: str
    capability: str
    scope: str


class TuiAction(StrictProtocolModel):
    kind: TuiActionKind
    target_id: str | None = None


class TuiSnapshot(StrictProtocolModel):
    focus: TuiFocus
    selected_index: int = Field(ge=0)
    latest_sequence: int = Field(ge=0)
    resync_required: bool
    status_message: str
    events: tuple[TuiEventRow, ...] = Field(max_length=MAXIMUM_TUI_EVENTS)
    tasks: tuple[TuiTaskRow, ...] = Field(max_length=MAXIMUM_TUI_TASKS)
    approvals: tuple[TuiApprovalRow, ...] = Field(max_length=MAXIMUM_TUI_APPROVALS)


class BoundedTuiProjection:
    """Owns bounded rows and keyboard state; it never stores event payloads."""

    def __init__(
        self,
        *,
        maximum_events: int = MAXIMUM_TUI_EVENTS,
        maximum_tasks: int = MAXIMUM_TUI_TASKS,
        maximum_approvals: int = MAXIMUM_TUI_APPROVALS,
    ) -> None:
        if not 1 <= maximum_events <= MAXIMUM_TUI_EVENTS:
            raise ValueError("maximum_events is outside TUI bounds")
        if not 1 <= maximum_tasks <= MAXIMUM_TUI_TASKS:
            raise ValueError("maximum_tasks is outside TUI bounds")
        if not 1 <= maximum_approvals <= MAXIMUM_TUI_APPROVALS:
            raise ValueError("maximum_approvals is outside TUI bounds")
        self._maximum_events = maximum_events
        self._maximum_tasks = maximum_tasks
        self._maximum_approvals = maximum_approvals
        self._events: deque[TuiEventRow] = deque()
        self._tasks: dict[str, TuiTaskRow] = {}
        self._approvals: dict[str, TuiApprovalRow] = {}
        self._focus = TuiFocus.EVENTS
        self._selected_index = 0
        self._latest_sequence = 0
        self._resync_required = False
        self._status_message = "connected"

    def apply_delivery(self, delivery: SequencedEvent | ResyncRequired) -> None:
        if isinstance(delivery, ResyncRequired):
            self._resync_required = True
            self._status_message = "resync required"
            self._latest_sequence = max(
                self._latest_sequence,
                delivery.latest_observed_sequence,
            )
            return
        if delivery.journal_sequence <= self._latest_sequence:
            raise ValueError("TUI events must arrive in increasing order")
        self._latest_sequence = delivery.journal_sequence
        self._events.append(
            TuiEventRow(
                journal_sequence=delivery.journal_sequence,
                event_id=delivery.event.event_id,
                event_type=delivery.event.event_type,
            )
        )
        while len(self._events) > self._maximum_events:
            self._events.popleft()
        self._status_message = "connected"

    def apply_task(self, task: TaskNodeRecord) -> None:
        self._tasks[task.task_id] = TuiTaskRow(
            task_id=task.task_id,
            state=task.state,
            depth=task.depth,
        )
        self._trim_mapping(self._tasks, self._maximum_tasks)

    def apply_approval(self, approval: ApprovalRecord) -> None:
        self._approvals[approval.approval_id] = TuiApprovalRow(
            approval_id=approval.approval_id,
            state=approval.state,
            capability=approval.capability,
            scope=approval.scope,
        )
        self._trim_mapping(self._approvals, self._maximum_approvals)

    def handle_key(self, key: TuiKey) -> TuiAction:
        if key is TuiKey.NEXT_PANEL:
            self._focus = self._cycle_focus(1)
            self._selected_index = 0
            return TuiAction(kind=TuiActionKind.NONE)
        if key is TuiKey.PREVIOUS_PANEL:
            self._focus = self._cycle_focus(-1)
            self._selected_index = 0
            return TuiAction(kind=TuiActionKind.NONE)
        if key in {TuiKey.UP, TuiKey.DOWN}:
            self._move_selection(-1 if key is TuiKey.UP else 1)
            return TuiAction(kind=TuiActionKind.NONE)
        if key is TuiKey.RESYNC:
            if self._resync_required:
                self._status_message = "resync requested"
                return TuiAction(kind=TuiActionKind.RESYNC)
            return TuiAction(kind=TuiActionKind.NONE)
        target_id = self._selected_id()
        if key is TuiKey.ENTER:
            return TuiAction(kind=TuiActionKind.INSPECT, target_id=target_id)
        if self._focus is TuiFocus.APPROVALS and target_id is not None:
            if key is TuiKey.APPROVE:
                return TuiAction(kind=TuiActionKind.APPROVE, target_id=target_id)
            if key is TuiKey.DENY:
                return TuiAction(kind=TuiActionKind.DENY, target_id=target_id)
        if key is TuiKey.CANCEL and target_id is not None:
            return TuiAction(kind=TuiActionKind.CANCEL, target_id=target_id)
        return TuiAction(kind=TuiActionKind.NONE)

    def snapshot(self) -> TuiSnapshot:
        rows = self._rows_for_focus()
        selected_index = min(self._selected_index, max(0, len(rows) - 1))
        return TuiSnapshot(
            focus=self._focus,
            selected_index=selected_index,
            latest_sequence=self._latest_sequence,
            resync_required=self._resync_required,
            status_message=self._status_message,
            events=tuple(self._events),
            tasks=tuple(self._tasks.values()),
            approvals=tuple(self._approvals.values()),
        )

    def render_lines(self) -> tuple[str, ...]:
        snapshot = self.snapshot()
        lines = [
            f"Atlas Harness | {snapshot.status_message} | focus={snapshot.focus}",
            (
                f"Events {len(snapshot.events)} | Tasks {len(snapshot.tasks)} "
                f"| Approvals {len(snapshot.approvals)}"
            ),
        ]
        for index, row in enumerate(self._rows_for_focus()[: MAXIMUM_RENDER_LINES - 2]):
            marker = ">" if index == snapshot.selected_index else " "
            lines.append(f"{marker} {self._row_text(row)}")
        return tuple(line[:MAXIMUM_RENDER_LINE_BYTES] for line in lines)

    def _rows_for_focus(self) -> tuple[object, ...]:
        if self._focus is TuiFocus.EVENTS:
            return tuple(self._events)
        if self._focus is TuiFocus.TASKS:
            return tuple(self._tasks.values())
        return tuple(self._approvals.values())

    def _selected_id(self) -> str | None:
        rows = self._rows_for_focus()
        if not rows:
            return None
        selected = rows[min(self._selected_index, len(rows) - 1)]
        return getattr(selected, "event_id", None) or getattr(
            selected,
            "task_id",
            None,
        ) or getattr(selected, "approval_id", None)

    def _move_selection(self, delta: int) -> None:
        row_count = len(self._rows_for_focus())
        if row_count:
            self._selected_index = min(
                max(0, self._selected_index + delta),
                row_count - 1,
            )

    def _cycle_focus(self, delta: int) -> TuiFocus:
        focuses = tuple(TuiFocus)
        return focuses[(focuses.index(self._focus) + delta) % len(focuses)]

    @staticmethod
    def _trim_mapping(mapping: dict[str, _Row], maximum: int) -> None:
        while len(mapping) > maximum:
            oldest_key = next(iter(mapping))
            del mapping[oldest_key]

    @staticmethod
    def _row_text(row: object) -> str:
        if isinstance(row, TuiEventRow):
            return f"event {row.journal_sequence} {row.event_type} {row.event_id}"
        if isinstance(row, TuiTaskRow):
            return f"task {row.state} depth={row.depth} {row.task_id}"
        if isinstance(row, TuiApprovalRow):
            return f"approval {row.state} {row.capability} {row.approval_id}"
        return "unknown row"
