"""Bounded DAG cancellation, worker reconciliation, and recovery evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Protocol, Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedLabel,
    BoundedReason,
    Sha256,
    StrictProtocolModel,
    TaskGraphId,
    TaskId,
    UtcTimestamp,
)
from app.services.harness.scheduler.dag import TaskGraphDefinition
from app.services.harness.scheduler.durable import (
    DagNodeRecord,
    DagNodeStatus,
    DagStoreConflict,
    DurableDagStore,
    InMemoryDurableDagStore,
)

MAXIMUM_CANCELLATION_NODES = 1_024
MAXIMUM_RECOVERY_EVIDENCE = 4_096
MAXIMUM_RECOVERY_EVIDENCE_PAGE = 256


class DagRecoveryAction(StrEnum):
    CANCELLED = "cancelled"
    RECONCILED_COMPLETED = "reconciled_completed"
    RECONCILED_FAILED = "reconciled_failed"
    PAUSED_AMBIGUOUS = "paused_ambiguous"
    ALREADY_TERMINAL = "already_terminal"
    STALE_WORKER = "stale_worker"


class DagWorkerOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DagWorkerObservation(StrictProtocolModel):
    task_id: TaskId
    owner_id: BoundedLabel
    lease_generation: int = Field(ge=1)
    outcome: DagWorkerOutcome
    result_sha256: Sha256 | None = None
    failure_reason: BoundedLabel | None = None
    observed_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.outcome is DagWorkerOutcome.COMPLETED:
            if self.result_sha256 is None or self.failure_reason is not None:
                raise ValueError("completed observation requires only a result hash")
        elif self.outcome is DagWorkerOutcome.FAILED:
            if self.failure_reason is None or self.result_sha256 is not None:
                raise ValueError("failed observation requires only a failure reason")
        elif self.failure_reason is None:
            raise ValueError("unknown observation requires a reason")
        return self


def dag_recovery_evidence_sha256(
    *,
    graph_id: TaskGraphId,
    task_id: TaskId,
    action: DagRecoveryAction,
    owner_id: BoundedLabel | None,
    lease_generation: int,
    result_sha256: Sha256 | None,
    reason: BoundedReason | None,
    observed_at: datetime,
) -> str:
    payload = {
        "action": action.value,
        "graph_id": graph_id,
        "lease_generation": lease_generation,
        "observed_at": observed_at.isoformat(),
        "owner_id": owner_id,
        "reason": reason,
        "result_sha256": result_sha256,
        "task_id": task_id,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


class DagRecoveryEvidence(StrictProtocolModel):
    graph_id: TaskGraphId
    task_id: TaskId
    action: DagRecoveryAction
    owner_id: BoundedLabel | None = None
    lease_generation: int = Field(ge=0)
    result_sha256: Sha256 | None = None
    reason: BoundedReason | None = None
    observed_at: UtcTimestamp
    evidence_sha256: Sha256

    @model_validator(mode="after")
    def validate_evidence_hash(self) -> Self:
        expected = dag_recovery_evidence_sha256(
            graph_id=self.graph_id,
            task_id=self.task_id,
            action=self.action,
            owner_id=self.owner_id,
            lease_generation=self.lease_generation,
            result_sha256=self.result_sha256,
            reason=self.reason,
            observed_at=self.observed_at,
        )
        if self.evidence_sha256 != expected:
            raise ValueError("DAG recovery evidence hash is invalid")
        return self


def build_dag_recovery_evidence(
    *,
    graph_id: TaskGraphId,
    task_id: TaskId,
    action: DagRecoveryAction,
    owner_id: BoundedLabel | None,
    lease_generation: int,
    result_sha256: Sha256 | None,
    reason: BoundedReason | None,
    observed_at: datetime,
) -> DagRecoveryEvidence:
    return DagRecoveryEvidence(
        graph_id=graph_id,
        task_id=task_id,
        action=action,
        owner_id=owner_id,
        lease_generation=lease_generation,
        result_sha256=result_sha256,
        reason=reason,
        observed_at=observed_at,
        evidence_sha256=dag_recovery_evidence_sha256(
            graph_id=graph_id,
            task_id=task_id,
            action=action,
            owner_id=owner_id,
            lease_generation=lease_generation,
            result_sha256=result_sha256,
            reason=reason,
            observed_at=observed_at,
        ),
    )


class DagReconciliation(StrictProtocolModel):
    record: DagNodeRecord
    action: DagRecoveryAction
    evidence: DagRecoveryEvidence


class RecoverableDagStore(DurableDagStore, Protocol):
    async def cancel_nodes(
        self,
        task_ids: tuple[TaskId, ...],
        reason: BoundedReason,
        observed_at: datetime,
    ) -> tuple[DagReconciliation, ...]: ...

    async def reconcile_worker(
        self,
        observation: DagWorkerObservation,
    ) -> DagReconciliation: ...

    async def recovery_evidence(
        self,
        *,
        limit: int = MAXIMUM_RECOVERY_EVIDENCE_PAGE,
    ) -> tuple[DagRecoveryEvidence, ...]: ...


class DurableDagRecoveryCoordinator:
    """Coordinates graph-wide cancellation against an atomic recovery store."""

    def __init__(
        self,
        graph: TaskGraphDefinition,
        store: RecoverableDagStore,
    ) -> None:
        self._graph = TaskGraphDefinition.model_validate(graph.model_dump())
        self._store = store

    async def cancel_subgraph(
        self,
        root_task_id: TaskId,
        reason: BoundedReason,
        *,
        observed_at: datetime,
    ) -> tuple[DagReconciliation, ...]:
        task_ids = _descendant_task_ids(self._graph, root_task_id)
        return await self._store.cancel_nodes(task_ids, reason, observed_at)

    async def reconcile_worker(
        self,
        observation: DagWorkerObservation,
    ) -> DagReconciliation:
        return await self._store.reconcile_worker(observation)

    async def recovery_evidence(
        self,
        *,
        limit: int = MAXIMUM_RECOVERY_EVIDENCE_PAGE,
    ) -> tuple[DagRecoveryEvidence, ...]:
        return await self._store.recovery_evidence(limit=limit)


class InMemoryRecoverableDagStore(InMemoryDurableDagStore):
    """Bounded reference implementation for recovery and cancellation tests."""

    def __init__(self, graph: TaskGraphDefinition) -> None:
        super().__init__(graph)
        self._graph = TaskGraphDefinition.model_validate(graph.model_dump())
        self._evidence: list[DagRecoveryEvidence] = []

    async def cancel_nodes(
        self,
        task_ids: tuple[TaskId, ...],
        reason: BoundedReason,
        observed_at: datetime,
    ) -> tuple[DagReconciliation, ...]:
        normalized_ids = tuple(sorted(set(task_ids)))
        if not 1 <= len(normalized_ids) <= MAXIMUM_CANCELLATION_NODES:
            raise DagStoreConflict("DAG cancellation set exceeds bound")
        if len(normalized_ids) != len(task_ids):
            raise DagStoreConflict("DAG cancellation set contains duplicates")
        async with self._lock:
            records = tuple(self._records.get(task_id) for task_id in normalized_ids)
            if any(record is None for record in records):
                raise DagStoreConflict("DAG cancellation references an unknown node")
            reconciliations: list[DagReconciliation] = []
            for record in records:
                if record is None or record.status in TERMINAL_DAG_STATUSES:
                    continue
                updated = DagNodeRecord(
                    task_id=record.task_id,
                    status=DagNodeStatus.CANCELLED,
                    lease_generation=record.lease_generation,
                )
                evidence = build_dag_recovery_evidence(
                    graph_id=self._graph.graph_id,
                    task_id=record.task_id,
                    action=DagRecoveryAction.CANCELLED,
                    owner_id=record.owner_id,
                    lease_generation=record.lease_generation,
                    result_sha256=None,
                    reason=reason,
                    observed_at=observed_at,
                )
                self._append_evidence(evidence)
                self._records[record.task_id] = updated
                reconciliations.append(
                    DagReconciliation(
                        record=updated,
                        action=DagRecoveryAction.CANCELLED,
                        evidence=evidence,
                    )
                )
            return tuple(reconciliations)

    async def reconcile_worker(
        self,
        observation: DagWorkerObservation,
    ) -> DagReconciliation:
        async with self._lock:
            record = self._records.get(observation.task_id)
            if record is None:
                raise DagStoreConflict("DAG worker observation references unknown node")
            action = DagRecoveryAction.STALE_WORKER
            updated = record
            evidence_reason = observation.failure_reason
            if (
                record.status is DagNodeStatus.RUNNING
                and record.owner_id == observation.owner_id
                and record.lease_generation == observation.lease_generation
            ):
                if observation.outcome is DagWorkerOutcome.COMPLETED:
                    updated = DagNodeRecord(
                        task_id=record.task_id,
                        status=DagNodeStatus.COMPLETED,
                        lease_generation=record.lease_generation,
                        result_sha256=observation.result_sha256,
                    )
                    action = DagRecoveryAction.RECONCILED_COMPLETED
                    evidence_reason = None
                elif observation.outcome is DagWorkerOutcome.FAILED:
                    updated = DagNodeRecord(
                        task_id=record.task_id,
                        status=DagNodeStatus.FAILED,
                        lease_generation=record.lease_generation,
                        failure_reason=observation.failure_reason,
                    )
                    action = DagRecoveryAction.RECONCILED_FAILED
                else:
                    updated = DagNodeRecord(
                        task_id=record.task_id,
                        status=DagNodeStatus.PAUSED,
                        lease_generation=record.lease_generation,
                        failure_reason=observation.failure_reason,
                    )
                    action = DagRecoveryAction.PAUSED_AMBIGUOUS
                self._records[record.task_id] = updated
            elif _observation_matches_terminal(record, observation):
                action = DagRecoveryAction.ALREADY_TERMINAL
                evidence_reason = "worker result is already durable"
            else:
                evidence_reason = evidence_reason or "worker lease is no longer current"
            evidence = build_dag_recovery_evidence(
                graph_id=self._graph.graph_id,
                task_id=observation.task_id,
                action=action,
                owner_id=observation.owner_id,
                lease_generation=observation.lease_generation,
                result_sha256=observation.result_sha256,
                reason=evidence_reason,
                observed_at=observation.observed_at,
            )
            self._append_evidence(evidence)
            return DagReconciliation(record=updated, action=action, evidence=evidence)

    async def recovery_evidence(
        self,
        *,
        limit: int = MAXIMUM_RECOVERY_EVIDENCE_PAGE,
    ) -> tuple[DagRecoveryEvidence, ...]:
        if not 1 <= limit <= MAXIMUM_RECOVERY_EVIDENCE_PAGE:
            raise ValueError("recovery evidence page exceeds bound")
        async with self._lock:
            return tuple(self._evidence[-limit:])

    def _append_evidence(self, evidence: DagRecoveryEvidence) -> None:
        if any(
            item.evidence_sha256 == evidence.evidence_sha256
            for item in self._evidence
        ):
            return
        if len(self._evidence) >= MAXIMUM_RECOVERY_EVIDENCE:
            raise DagStoreConflict("DAG recovery evidence capacity is exhausted")
        self._evidence.append(evidence)


TERMINAL_DAG_STATUSES = frozenset(
    {
        DagNodeStatus.COMPLETED,
        DagNodeStatus.FAILED,
        DagNodeStatus.CANCELLED,
        DagNodeStatus.PAUSED,
    }
)


def _observation_matches_terminal(
    record: DagNodeRecord,
    observation: DagWorkerObservation,
) -> bool:
    if record.lease_generation != observation.lease_generation:
        return False
    if (
        record.status is DagNodeStatus.COMPLETED
        and observation.outcome is DagWorkerOutcome.COMPLETED
    ):
        return record.result_sha256 == observation.result_sha256
    if (
        record.status is DagNodeStatus.FAILED
        and observation.outcome is DagWorkerOutcome.FAILED
    ):
        return record.failure_reason == observation.failure_reason
    return False


def _descendant_task_ids(
    graph: TaskGraphDefinition,
    root_task_id: TaskId,
) -> tuple[TaskId, ...]:
    node_ids = {node.task_id for node in graph.nodes}
    if root_task_id not in node_ids:
        raise DagStoreConflict("DAG cancellation root does not exist")
    dependents: dict[TaskId, list[TaskId]] = {task_id: [] for task_id in node_ids}
    for node in graph.nodes:
        for dependency in node.dependency_task_ids:
            dependents[dependency].append(node.task_id)
    pending = [root_task_id]
    descendants: set[TaskId] = set()
    while pending:
        task_id = pending.pop()
        if task_id in descendants:
            continue
        descendants.add(task_id)
        pending.extend(sorted(dependents[task_id], reverse=True))
    return tuple(sorted(descendants))
