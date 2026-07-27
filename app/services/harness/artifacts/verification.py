"""Independent verification, bounded repair, and staged patch-conflict contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.artifacts.handoffs import HandoffChange
from app.services.harness.protocol.base import (
    ArtifactId,
    BoundedLabel,
    BoundedReason,
    ExecutionBudget,
    Sha256,
    StrictProtocolModel,
    TaskId,
    UtcTimestamp,
    WorkspaceId,
)

MAXIMUM_VERIFIER_RUNS = 1_024
MAXIMUM_REPAIR_ATTEMPTS = 3
MAXIMUM_PATCH_PROPOSALS = 256


class VerifierStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class RepairState(StrEnum):
    REQUESTED = "requested"
    COMPLETED = "completed"
    REJECTED = "rejected"


class PatchConflictState(StrEnum):
    STAGED = "staged"
    RESOLVED = "resolved"


class VerifierRun(StrictProtocolModel):
    handoff_id: ArtifactId
    producer_task_id: TaskId
    verifier_task_id: TaskId
    gate: BoundedLabel
    status: VerifierStatus
    evidence_artifact_ids: tuple[ArtifactId, ...] = Field(max_length=256)
    reason: BoundedReason | None = None
    observed_at: UtcTimestamp
    run_sha256: Sha256

    @model_validator(mode="after")
    def validate_run(self) -> Self:
        if self.producer_task_id == self.verifier_task_id:
            raise ValueError("verifier must be independent from producer")
        _require_sorted_unique(self.evidence_artifact_ids, "verifier evidence")
        if self.status is VerifierStatus.PASSED:
            if not self.evidence_artifact_ids or self.reason is not None:
                raise ValueError("passing verifier run requires evidence only")
        elif self.reason is None:
            raise ValueError("failed verifier run requires a reason")
        expected = verifier_run_sha256(self.model_dump(mode="json"))
        if self.run_sha256 != expected:
            raise ValueError("verifier run hash is invalid")
        return self


class VerificationAcceptance(StrictProtocolModel):
    handoff_id: ArtifactId
    accepted: bool
    run_sha256s: tuple[Sha256, ...] = Field(max_length=64)
    reason: BoundedReason | None = None

    @model_validator(mode="after")
    def validate_acceptance(self) -> Self:
        _require_sorted_unique(self.run_sha256s, "verification runs")
        if self.accepted != (self.reason is None):
            raise ValueError("acceptance reason must agree with outcome")
        return self


class RepairRequest(StrictProtocolModel):
    handoff_id: ArtifactId
    producer_task_id: TaskId
    verifier_task_id: TaskId
    attempt: int = Field(ge=1, le=MAXIMUM_REPAIR_ATTEMPTS)
    maximum_attempts: int = Field(ge=1, le=MAXIMUM_REPAIR_ATTEMPTS)
    budget: ExecutionBudget
    reason: BoundedReason
    requested_at: UtcTimestamp
    repair_id: ArtifactId

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.producer_task_id == self.verifier_task_id:
            raise ValueError("repair verifier must be independent")
        if self.attempt > self.maximum_attempts:
            raise ValueError("repair attempt exceeds maximum")
        expected = repair_request_sha256(self)
        if self.repair_id != expected:
            raise ValueError("repair request ID is invalid")
        return self


class RepairLedgerEntry(StrictProtocolModel):
    request: RepairRequest
    state: RepairState
    completed_artifact_id: ArtifactId | None = None
    outcome_reason: BoundedReason | None = None

    @model_validator(mode="after")
    def validate_entry(self) -> Self:
        if self.state is RepairState.COMPLETED and self.completed_artifact_id is None:
            raise ValueError("completed repair requires an artifact")
        if self.state is RepairState.REJECTED and self.outcome_reason is None:
            raise ValueError("rejected repair requires a reason")
        return self


class PatchProposal(StrictProtocolModel):
    proposal_id: ArtifactId
    producer_task_id: TaskId
    workspace_id: WorkspaceId
    changes: tuple[HandoffChange, ...] = Field(min_length=1, max_length=256)
    proposal_sha256: Sha256

    @model_validator(mode="after")
    def validate_proposal(self) -> Self:
        paths = tuple(change.path for change in self.changes)
        _require_sorted_unique(paths, "patch proposal paths")
        expected = patch_proposal_sha256(self.model_dump(mode="json"))
        if self.proposal_sha256 != expected:
            raise ValueError("patch proposal hash is invalid")
        return self


class PatchConflict(StrictProtocolModel):
    conflict_id: ArtifactId
    workspace_id: WorkspaceId
    path: str
    proposal_ids: tuple[ArtifactId, ...] = Field(min_length=2, max_length=16)
    state: PatchConflictState
    resolved_proposal_id: ArtifactId | None = None
    verifier_task_id: TaskId | None = None
    conflict_sha256: Sha256

    @model_validator(mode="after")
    def validate_conflict(self) -> Self:
        _require_sorted_unique(self.proposal_ids, "conflict proposals")
        resolved = self.state is PatchConflictState.RESOLVED
        if resolved != (
            self.resolved_proposal_id is not None and self.verifier_task_id is not None
        ):
            raise ValueError("resolved conflict requires proposal and verifier")
        if self.resolved_proposal_id is not None and self.resolved_proposal_id not in (
            self.proposal_ids
        ):
            raise ValueError("resolved proposal is not part of conflict")
        expected = patch_conflict_sha256(self.model_dump(mode="json"))
        if self.conflict_sha256 != expected:
            raise ValueError("patch conflict hash is invalid")
        return self


class VerifierLedger:
    """Independent verification ledger with bounded repair scheduling."""

    def __init__(self) -> None:
        self._runs: dict[str, VerifierRun] = {}
        self._repairs: dict[str, RepairLedgerEntry] = {}

    def record_run(self, run: VerifierRun) -> VerifierRun:
        if (
            run.run_sha256 not in self._runs
            and len(self._runs) >= MAXIMUM_VERIFIER_RUNS
        ):
            raise ValueError("verifier run capacity is exhausted")
        existing = self._runs.get(run.run_sha256)
        if existing is not None and existing != run:
            raise ValueError("verifier run identity collision")
        self._runs[run.run_sha256] = run
        return run

    def accept(
        self,
        handoff_id: ArtifactId,
        required_gates: tuple[str, ...],
    ) -> VerificationAcceptance:
        _require_sorted_unique(required_gates, "required verifier gates")
        runs = tuple(
            run
            for run in self._runs.values()
            if run.handoff_id == handoff_id
        )
        selected = tuple(
            run
            for gate in required_gates
            for run in runs
            if run.gate == gate
        )
        passed_gates = {
            run.gate for run in selected if run.status is VerifierStatus.PASSED
        }
        accepted = passed_gates == set(required_gates) and len(
            selected
        ) == len(required_gates)
        return VerificationAcceptance(
            handoff_id=handoff_id,
            accepted=accepted,
            run_sha256s=tuple(sorted(run.run_sha256 for run in selected)),
            reason=None if accepted else "independent verifier gates are incomplete",
        )

    def request_repair(self, request: RepairRequest) -> RepairLedgerEntry:
        if request.repair_id in self._repairs:
            return self._repairs[request.repair_id]
        if len(self._repairs) >= MAXIMUM_VERIFIER_RUNS:
            raise ValueError("repair ledger capacity is exhausted")
        entry = RepairLedgerEntry(request=request, state=RepairState.REQUESTED)
        self._repairs[request.repair_id] = entry
        return entry

    def complete_repair(
        self,
        repair_id: ArtifactId,
        artifact_id: ArtifactId,
    ) -> RepairLedgerEntry:
        entry = self._repairs.get(repair_id)
        if entry is None:
            raise KeyError("repair request does not exist")
        updated = RepairLedgerEntry(
            request=entry.request,
            state=RepairState.COMPLETED,
            completed_artifact_id=artifact_id,
        )
        self._repairs[repair_id] = updated
        return updated


class InMemoryPatchConflictResolver:
    """Stages overlapping paths and never silently chooses a producer."""

    def __init__(self) -> None:
        self._proposals: dict[str, PatchProposal] = {}
        self._conflicts: dict[str, PatchConflict] = {}

    def stage(self, proposals: tuple[PatchProposal, ...]) -> tuple[PatchConflict, ...]:
        if not 1 <= len(proposals) <= MAXIMUM_PATCH_PROPOSALS:
            raise ValueError("patch proposal batch exceeds bound")
        workspaces = {proposal.workspace_id for proposal in proposals}
        if len(workspaces) != 1:
            raise ValueError("patch proposals must share one workspace")
        for proposal in proposals:
            self._proposals[proposal.proposal_id] = proposal
        by_path: dict[str, list[str]] = {}
        for proposal in proposals:
            for change in proposal.changes:
                by_path.setdefault(change.path, []).append(proposal.proposal_id)
        conflicts: list[PatchConflict] = []
        for path, proposal_ids in sorted(by_path.items()):
            unique_ids = tuple(sorted(set(proposal_ids)))
            if len(unique_ids) < 2:
                continue
            conflict_id = "art_" + hashlib.sha256(
                (path + ":" + ":".join(unique_ids)).encode()
            ).hexdigest()[:32]
            conflict = PatchConflict(
                conflict_id=conflict_id,
                workspace_id=proposals[0].workspace_id,
                path=path,
                proposal_ids=unique_ids,
                state=PatchConflictState.STAGED,
                conflict_sha256=patch_conflict_sha256(
                    {
                        "conflict_id": conflict_id,
                        "workspace_id": proposals[0].workspace_id,
                        "path": path,
                        "proposal_ids": list(unique_ids),
                        "state": PatchConflictState.STAGED.value,
                        "resolved_proposal_id": None,
                        "verifier_task_id": None,
                    }
                ),
            )
            self._conflicts[conflict_id] = conflict
            conflicts.append(conflict)
        return tuple(conflicts)

    def resolve(
        self,
        conflict_id: ArtifactId,
        selected_proposal_id: ArtifactId,
        verifier_task_id: TaskId,
    ) -> PatchConflict:
        conflict = self._conflicts.get(conflict_id)
        if conflict is None:
            raise KeyError("patch conflict does not exist")
        if conflict.state is PatchConflictState.RESOLVED:
            return conflict
        if selected_proposal_id not in conflict.proposal_ids:
            raise ValueError("selected proposal is not part of conflict")
        producer_ids = {
            self._proposals[proposal_id].producer_task_id
            for proposal_id in conflict.proposal_ids
        }
        if verifier_task_id in producer_ids:
            raise ValueError("conflict verifier must be independent")
        values = conflict.model_dump(mode="python")
        values.update(
            {
                "state": PatchConflictState.RESOLVED,
                "resolved_proposal_id": selected_proposal_id,
                "verifier_task_id": verifier_task_id,
            }
        )
        values.pop("conflict_sha256")
        resolved_hash = patch_conflict_sha256(
            {
                **conflict.model_dump(mode="json"),
                "state": PatchConflictState.RESOLVED.value,
                "resolved_proposal_id": selected_proposal_id,
                "verifier_task_id": verifier_task_id,
            }
        )
        resolved = PatchConflict(**values, conflict_sha256=resolved_hash)
        self._conflicts[conflict_id] = resolved
        return resolved


def verifier_run_sha256(values: object) -> str:
    return _hash_without(values, "run_sha256")


def repair_request_sha256(request: RepairRequest) -> str:
    return "art_" + _hash_without(request.model_dump(mode="json"), "repair_id")[:32]


def patch_proposal_sha256(values: object) -> str:
    return _hash_without(values, "proposal_sha256")


def patch_conflict_sha256(values: object) -> str:
    return _hash_without(values, "conflict_sha256")


def _hash_without(values: object, field_name: str) -> str:
    if isinstance(values, dict):
        values = {
            key: value for key, value in values.items() if key != field_name
        }
    return hashlib.sha256(
        json.dumps(
            values,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _require_sorted_unique(values: Iterable[str], field_name: str) -> None:
    values_tuple = tuple(values)
    if tuple(sorted(set(values_tuple))) != values_tuple:
        raise ValueError(f"{field_name} must be unique and sorted")
