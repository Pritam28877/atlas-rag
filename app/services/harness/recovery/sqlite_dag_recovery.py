"""SQLite transaction helpers for DAG recovery state and evidence."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from app.services.harness.protocol.base import BoundedReason, TaskId
from app.services.harness.scheduler.dag import TaskGraphDefinition
from app.services.harness.scheduler.durable import (
    DagNodeRecord,
    DagNodeStatus,
    DagStoreConflict,
)
from app.services.harness.scheduler.recovery import (
    MAXIMUM_CANCELLATION_NODES,
    MAXIMUM_RECOVERY_EVIDENCE,
    TERMINAL_DAG_STATUSES,
    DagReconciliation,
    DagRecoveryAction,
    DagRecoveryEvidence,
    DagWorkerObservation,
    DagWorkerOutcome,
    build_dag_recovery_evidence,
)


class SQLiteDagRecoveryMixin:
    """Private transaction operations mixed into the public DAG adapter."""

    _graph: TaskGraphDefinition

    def _cancel_nodes(
        self,
        connection: sqlite3.Connection,
        task_ids: tuple[TaskId, ...],
        reason: BoundedReason,
        observed_at: datetime,
    ) -> tuple[DagReconciliation, ...]:
        normalized_ids = tuple(sorted(set(task_ids)))
        if not 1 <= len(normalized_ids) <= MAXIMUM_CANCELLATION_NODES:
            raise DagStoreConflict("DAG cancellation set exceeds bound")
        if len(normalized_ids) != len(task_ids):
            raise DagStoreConflict("DAG cancellation set contains duplicates")
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = tuple(
                connection.execute(
                    """
                    SELECT task_id, status, owner_id, lease_generation,
                           lease_expires_at, result_sha256, failure_reason
                    FROM harness_dag_nodes
                    WHERE graph_id = ? AND task_id = ?
                    """,
                    (self._graph.graph_id, task_id),
                ).fetchone()
                for task_id in normalized_ids
            )
            if any(row is None for row in rows):
                raise DagStoreConflict("DAG cancellation references unknown node")
            reconciliations: list[DagReconciliation] = []
            for row in rows:
                if row is None:
                    continue
                record = dag_row_to_record(row)
                if record.status in TERMINAL_DAG_STATUSES:
                    continue
                updated = connection.execute(
                    """
                    UPDATE harness_dag_nodes
                    SET status = ?, owner_id = NULL, lease_expires_at = NULL,
                        result_sha256 = NULL, failure_reason = NULL
                    WHERE graph_id = ? AND task_id = ? AND status = ?
                      AND lease_generation = ?
                    """,
                    (
                        DagNodeStatus.CANCELLED.value,
                        self._graph.graph_id,
                        record.task_id,
                        record.status.value,
                        record.lease_generation,
                    ),
                )
                if updated.rowcount != 1:
                    raise DagStoreConflict("DAG cancellation fence rejected")
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
                self._insert_evidence(connection, evidence)
                reconciliations.append(
                    DagReconciliation(
                        record=DagNodeRecord(
                            task_id=record.task_id,
                            status=DagNodeStatus.CANCELLED,
                            lease_generation=record.lease_generation,
                        ),
                        action=DagRecoveryAction.CANCELLED,
                        evidence=evidence,
                    )
                )
            connection.commit()
            return tuple(reconciliations)
        except BaseException:
            connection.rollback()
            raise

    def _reconcile_worker(
        self,
        connection: sqlite3.Connection,
        observation: DagWorkerObservation,
    ) -> DagReconciliation:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                """
                SELECT task_id, status, owner_id, lease_generation,
                       lease_expires_at, result_sha256, failure_reason
                FROM harness_dag_nodes
                WHERE graph_id = ? AND task_id = ?
                """,
                (self._graph.graph_id, observation.task_id),
            ).fetchone()
            if row is None:
                raise DagStoreConflict("DAG worker observation references unknown node")
            record = dag_row_to_record(row)
            action = DagRecoveryAction.STALE_WORKER
            updated_record = record
            evidence_reason = observation.failure_reason
            if (
                record.status is DagNodeStatus.RUNNING
                and record.owner_id == observation.owner_id
                and record.lease_generation == observation.lease_generation
            ):
                if observation.outcome is DagWorkerOutcome.COMPLETED:
                    status = DagNodeStatus.COMPLETED
                    result_sha256 = observation.result_sha256
                    failure_reason = None
                    action = DagRecoveryAction.RECONCILED_COMPLETED
                    evidence_reason = None
                elif observation.outcome is DagWorkerOutcome.FAILED:
                    status = DagNodeStatus.FAILED
                    result_sha256 = None
                    failure_reason = observation.failure_reason
                    action = DagRecoveryAction.RECONCILED_FAILED
                else:
                    status = DagNodeStatus.PAUSED
                    result_sha256 = None
                    failure_reason = observation.failure_reason
                    action = DagRecoveryAction.PAUSED_AMBIGUOUS
                updated = connection.execute(
                    """
                    UPDATE harness_dag_nodes
                    SET status = ?, owner_id = NULL, lease_expires_at = NULL,
                        result_sha256 = ?, failure_reason = ?
                    WHERE graph_id = ? AND task_id = ? AND status = ?
                      AND owner_id = ? AND lease_generation = ?
                    """,
                    (
                        status.value,
                        result_sha256,
                        failure_reason,
                        self._graph.graph_id,
                        observation.task_id,
                        DagNodeStatus.RUNNING.value,
                        observation.owner_id,
                        observation.lease_generation,
                    ),
                )
                if updated.rowcount != 1:
                    raise DagStoreConflict("DAG worker reconciliation fence rejected")
                updated_record = DagNodeRecord(
                    task_id=record.task_id,
                    status=status,
                    lease_generation=record.lease_generation,
                    result_sha256=result_sha256,
                    failure_reason=failure_reason,
                )
            elif observation_matches_terminal(record, observation):
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
            self._insert_evidence(connection, evidence)
            connection.commit()
            return DagReconciliation(
                record=updated_record,
                action=action,
                evidence=evidence,
            )
        except BaseException:
            connection.rollback()
            raise

    def _read_evidence(
        self,
        connection: sqlite3.Connection,
        limit: int,
    ) -> tuple[DagRecoveryEvidence, ...]:
        rows = connection.execute(
            """
            SELECT evidence_sha256, graph_id, task_id, action, owner_id,
                   lease_generation, result_sha256, reason, observed_at
            FROM harness_dag_recovery_evidence
            WHERE graph_id = ?
            ORDER BY observed_at, evidence_sha256
            LIMIT ?
            """,
            (self._graph.graph_id, limit),
        ).fetchall()
        return tuple(row_to_evidence(row) for row in rows)

    def _insert_evidence(
        self,
        connection: sqlite3.Connection,
        evidence: DagRecoveryEvidence,
    ) -> None:
        existing = connection.execute(
            """
            SELECT 1 FROM harness_dag_recovery_evidence
            WHERE evidence_sha256 = ?
            """,
            (evidence.evidence_sha256,),
        ).fetchone()
        if existing is not None:
            return
        count = connection.execute(
            """
            SELECT COUNT(*) AS evidence_count
            FROM harness_dag_recovery_evidence
            WHERE graph_id = ?
            """,
            (self._graph.graph_id,),
        ).fetchone()
        if count is None or int(count["evidence_count"]) >= MAXIMUM_RECOVERY_EVIDENCE:
            raise DagStoreConflict("DAG recovery evidence capacity is exhausted")
        connection.execute(
            """
            INSERT INTO harness_dag_recovery_evidence (
                evidence_sha256, graph_id, task_id, action, owner_id,
                lease_generation, result_sha256, reason, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence.evidence_sha256,
                evidence.graph_id,
                evidence.task_id,
                evidence.action.value,
                evidence.owner_id,
                evidence.lease_generation,
                evidence.result_sha256,
                evidence.reason,
                evidence.observed_at.isoformat(),
            ),
        )


def dag_row_to_record(row: sqlite3.Row) -> DagNodeRecord:
    return DagNodeRecord(
        task_id=str(row["task_id"]),
        status=DagNodeStatus(str(row["status"])),
        owner_id=row["owner_id"],
        lease_generation=int(row["lease_generation"]),
        lease_expires_at=parse_dag_datetime(row["lease_expires_at"]),
        result_sha256=row["result_sha256"],
        failure_reason=row["failure_reason"],
    )


def observation_matches_terminal(
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


def row_to_evidence(row: sqlite3.Row) -> DagRecoveryEvidence:
    observed_at = parse_dag_datetime(row["observed_at"])
    if observed_at is None:
        raise DagStoreConflict("DAG recovery evidence timestamp is missing")
    return DagRecoveryEvidence(
        evidence_sha256=str(row["evidence_sha256"]),
        graph_id=str(row["graph_id"]),
        task_id=str(row["task_id"]),
        action=DagRecoveryAction(str(row["action"])),
        owner_id=row["owner_id"],
        lease_generation=int(row["lease_generation"]),
        result_sha256=row["result_sha256"],
        reason=row["reason"],
        observed_at=observed_at,
    )


def parse_dag_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DagStoreConflict("DAG lease timestamp is invalid")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise DagStoreConflict("DAG lease timestamp is invalid") from error
