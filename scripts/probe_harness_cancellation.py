"""Exercise real provider/process cancellation and durable operator evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from app.services.harness.journal import (
    AppendStatus,
    JournalReadRequest,
    SQLiteEventJournal,
    SQLiteJournalIntegrityVerifier,
)
from app.services.harness.protocol import InlinePayload
from app.services.harness.sessions import CancellationCoordinator
from scripts.harness_cancellation_drill_support import (
    WORKSPACE_ID,
    JournalCancellationEvidence,
    ProcessGroupResource,
    ProviderTaskResource,
    read_process_ids,
    wait_for_processes_gone,
)

ROOT = Path(__file__).resolve().parents[1]
PROCESS_WORKER = ROOT / "scripts" / "harness_cancellation_process.py"
SUCCESS_TURN_ID = "trn_" + "3" * 32
FAILURE_TURN_ID = "trn_" + "4" * 32


async def cooperative_provider(started: asyncio.Event) -> None:
    started.set()
    await asyncio.Event().wait()


async def stubborn_provider(
    started: asyncio.Event,
    allow_cleanup: asyncio.Event,
) -> None:
    started.set()
    while True:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if allow_cleanup.is_set():
                raise


async def launch_process_tree(
    pid_path: Path,
) -> tuple[ProcessGroupResource, tuple[int, int]]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(PROCESS_WORKER),
        "--mode",
        "parent",
        "--pid-path",
        str(pid_path),
        cwd=pid_path.parent,
        env={"PYTHONIOENCODING": "utf-8"},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    resource = ProcessGroupResource("drill-process-tree", process)
    try:
        process_ids = await read_process_ids(pid_path)
    except BaseException:
        resource.force_terminate()
        await resource.wait_settled()
        raise
    return resource, process_ids


async def read_evidence(
    journal: SQLiteEventJournal,
    turn_id: str,
) -> tuple[str, str]:
    page = await journal.read_aggregate(
        JournalReadRequest(
            workspace_id=WORKSPACE_ID,
            aggregate_id=turn_id,
            after_sequence=0,
        )
    )
    if len(page.events) != 1:
        raise RuntimeError("cancellation evidence must contain exactly one event")
    event = page.events[0]
    if not isinstance(event.payload, InlinePayload):
        raise RuntimeError("drill cancellation evidence must remain inline")
    return event.event_type, event.payload.text


async def run_success_drill(
    journal: SQLiteEventJournal,
    evidence: JournalCancellationEvidence,
    pid_path: Path,
) -> dict[str, object]:
    provider_started = asyncio.Event()
    provider_task = asyncio.create_task(
        cooperative_provider(provider_started)
    )
    process: ProcessGroupResource | None = None
    process_ids: tuple[int, int] | None = None
    try:
        await provider_started.wait()
        provider = ProviderTaskResource("cooperative-provider", provider_task)
        process, process_ids = await launch_process_tree(pid_path)
        coordinator = CancellationCoordinator(SUCCESS_TURN_ID, evidence)
        await coordinator.register(provider)
        await coordinator.register(process)
        report = await coordinator.cancel(
            "Runtime cancellation drill.",
            evidence_timeout_seconds=1,
            cooperative_settle_timeout_seconds=0.05,
            forced_settle_timeout_seconds=1,
        )
        await wait_for_processes_gone(process_ids)
        event_type, payload = await read_evidence(
            journal,
            SUCCESS_TURN_ID,
        )
        return {
            "active_resources": await coordinator.active_resources(),
            "event_type": event_type,
            "evidence_matches": payload == report.model_dump_json(),
            "process_returncode": process.returncode,
            "provider_done": provider_task.done(),
            "settlements": {
                result.identity.kind.value: result.settlement.value
                for result in report.resources
            },
            "state": report.state.value,
        }
    finally:
        provider_task.cancel()
        await asyncio.gather(provider_task, return_exceptions=True)
        if process is not None and process.returncode is None:
            process.force_terminate()
            await process.wait_settled()
        if process_ids is not None:
            await wait_for_processes_gone(process_ids)


async def run_failure_drill(
    journal: SQLiteEventJournal,
    evidence: JournalCancellationEvidence,
) -> dict[str, object]:
    stubborn_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    stubborn_task = asyncio.create_task(
        stubborn_provider(stubborn_started, allow_cleanup)
    )
    try:
        await stubborn_started.wait()
        stubborn = ProviderTaskResource("stubborn-provider", stubborn_task)
        coordinator = CancellationCoordinator(FAILURE_TURN_ID, evidence)
        await coordinator.register(stubborn)
        report = await coordinator.cancel(
            "Containment failure drill.",
            evidence_timeout_seconds=1,
            cooperative_settle_timeout_seconds=0.05,
            forced_settle_timeout_seconds=0.05,
        )
        event_type, payload = await read_evidence(
            journal,
            FAILURE_TURN_ID,
        )
        failure_summary: dict[str, object] = {
            "active_resources_at_evidence": (
                await coordinator.active_resources()
            ),
            "event_type": event_type,
            "evidence_matches": payload == report.model_dump_json(),
            "provider_pending_at_evidence": not stubborn_task.done(),
            "settlement": report.resources[0].settlement.value,
            "state": report.state.value,
        }
    finally:
        allow_cleanup.set()
        stubborn_task.cancel()
        await asyncio.gather(stubborn_task, return_exceptions=True)
    failure_summary["provider_cleaned"] = stubborn_task.done()
    return failure_summary


async def verify_journal(database_path: Path) -> bool:
    verifier = await SQLiteJournalIntegrityVerifier.open(database_path)
    try:
        verification = await verifier.verify()
    finally:
        await verifier.close()
    return verification.complete


async def run_probe(database_path: Path, pid_path: Path) -> dict[str, object]:
    journal = await SQLiteEventJournal.open(database_path)
    evidence = JournalCancellationEvidence(journal)
    try:
        success_summary = await run_success_drill(
            journal,
            evidence,
            pid_path,
        )
        failure_summary = await run_failure_drill(journal, evidence)
    finally:
        await journal.close()
    return {
        "evidence_appended": evidence.statuses
        == [AppendStatus.APPENDED, AppendStatus.APPENDED],
        "failure": failure_summary,
        "journal_verified": await verify_journal(database_path),
        "success": success_summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-path", required=True, type=Path)
    parser.add_argument("--pid-path", required=True, type=Path)
    arguments = parser.parse_args()
    os.umask(0o077)
    report = asyncio.run(
        run_probe(arguments.database_path, arguments.pid_path)
    )
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
