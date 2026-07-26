"""Real resource adapters and durable evidence for the cancellation drill."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
from pathlib import Path

from app.services.harness.journal import (
    AppendRequest,
    AppendStatus,
    EventJournal,
    JournalDurability,
)
from app.services.harness.protocol import (
    CanonicalEventType,
    EventActorKind,
    EventRecord,
    InlinePayload,
    TraceLink,
)
from app.services.harness.sessions import (
    CancellationReport,
    CancellationResourceIdentity,
    CancellationResourceKind,
    CancellationScopeState,
)

WORKSPACE_ID = "wsp_" + "1" * 32
SYSTEM_PRINCIPAL_ID = "prn_" + "2" * 32


def resource_identity(
    kind: CancellationResourceKind,
    label: str,
) -> CancellationResourceIdentity:
    digest = hashlib.sha256(f"{kind.value}:{label}".encode()).hexdigest()
    return CancellationResourceIdentity(resource_id=digest, kind=kind)


def inline_payload(text: str) -> InlinePayload:
    encoded = text.encode()
    return InlinePayload(
        text=text,
        size_bytes=len(encoded),
        content_sha256=hashlib.sha256(encoded).hexdigest(),
    )


class ProviderTaskResource:
    def __init__(self, label: str, task: asyncio.Task[None]) -> None:
        self._identity = resource_identity(
            CancellationResourceKind.PROVIDER_REQUEST,
            label,
        )
        self._task = task

    @property
    def cancellation_identity(self) -> CancellationResourceIdentity:
        return self._identity

    def request_cancel(self) -> None:
        self._task.cancel()

    def force_terminate(self) -> None:
        self._task.cancel()

    async def wait_settled(self) -> None:
        try:
            await asyncio.shield(self._task)
        except asyncio.CancelledError:
            if self._task.done():
                return
            raise
        except Exception:
            return


class ProcessGroupResource:
    def __init__(
        self,
        label: str,
        process: asyncio.subprocess.Process,
    ) -> None:
        self._identity = resource_identity(
            CancellationResourceKind.PROCESS_TREE,
            label,
        )
        self._process = process
        self._wait_task = asyncio.create_task(process.wait())

    @property
    def cancellation_identity(self) -> CancellationResourceIdentity:
        return self._identity

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def request_cancel(self) -> None:
        self._signal_group(signal.SIGTERM)

    def force_terminate(self) -> None:
        self._signal_group(signal.SIGKILL)

    async def wait_settled(self) -> None:
        try:
            await asyncio.shield(self._wait_task)
        except asyncio.CancelledError:
            if self._wait_task.done():
                return
            raise

    def _signal_group(self, requested_signal: signal.Signals) -> None:
        if self._process.returncode is not None:
            return
        try:
            os.killpg(self._process.pid, requested_signal)
        except ProcessLookupError:
            return


class JournalCancellationEvidence:
    def __init__(self, journal: EventJournal) -> None:
        self._journal = journal
        self.statuses: list[AppendStatus] = []

    async def record_cancellation(self, report: CancellationReport) -> None:
        serialized_report = report.model_dump_json()
        report_digest = hashlib.sha256(serialized_report.encode()).hexdigest()
        event_id = f"evt_{report_digest[:32]}"
        event_type = (
            CanonicalEventType.TURN_CANCELLED.value
            if report.state is CancellationScopeState.CANCELLED
            else CanonicalEventType.TURN_FAILED.value
        )
        event = EventRecord(
            event_id=event_id,
            event_type=event_type,
            schema_version="1.2",
            aggregate_id=report.turn_id,
            aggregate_sequence=1,
            actor_kind=EventActorKind.SYSTEM,
            actor_principal_id=SYSTEM_PRINCIPAL_ID,
            occurred_at=report.completed_at,
            trace=TraceLink(
                request_id=f"req_{report_digest[32:64]}",
                correlation_id=event_id,
            ),
            payload=inline_payload(serialized_report),
        )
        append_result = await self._journal.append(
            AppendRequest(
                workspace_id=WORKSPACE_ID,
                aggregate_id=report.turn_id,
                expected_sequence=0,
                idempotency_key=f"cancel-evidence:{report.turn_id}",
                request_sha256=report_digest,
                durability=JournalDurability.SYNCHRONOUS,
                events=(event,),
            )
        )
        self.statuses.append(append_result.status)


async def read_process_ids(
    pid_path: Path,
    *,
    timeout_seconds: float = 2,
) -> tuple[int, int]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw_payload = await asyncio.to_thread(
                pid_path.read_text,
                encoding="utf-8",
            )
        except FileNotFoundError:
            await asyncio.sleep(0.01)
            continue
        payload = json.loads(raw_payload)
        return int(payload["parent_pid"]), int(payload["child_pid"])
    raise TimeoutError("process tree did not publish its bounded PID evidence")


async def wait_for_processes_gone(
    process_ids: tuple[int, ...],
    *,
    timeout_seconds: float = 2,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    process_paths = tuple(Path("/proc") / str(pid) for pid in process_ids)
    while asyncio.get_running_loop().time() < deadline:
        processes_exist = await asyncio.to_thread(
            _processes_exist,
            process_paths,
        )
        if not processes_exist:
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("cancelled process tree remains visible in /proc")


def _processes_exist(process_paths: tuple[Path, ...]) -> bool:
    return any(process_path.exists() for process_path in process_paths)
