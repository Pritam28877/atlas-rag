"""SQLite-backed durable DAG leases and fenced checkpoints."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from app.services.harness.journal.sqlite_connection import SQLiteConnectionOwner
from app.services.harness.protocol.base import BoundedLabel, Sha256, TaskId
from app.services.harness.scheduler.dag import TaskGraphDefinition
from app.services.harness.scheduler.durable import (
    DagLease,
    DagNodeRecord,
    DagNodeStatus,
    DagStoreConflict,
)


class SQLiteDurableDagStore:
    """Durable store adapter; all mutations use one serialized SQLite owner."""

    def __init__(
        self,
        connection_owner: SQLiteConnectionOwner,
        graph: TaskGraphDefinition,
    ) -> None:
        self._connection_owner = connection_owner
        self._graph = TaskGraphDefinition.model_validate(graph.model_dump())

    async def initialize(self) -> None:
        await self._connection_owner.execute(self._initialize)

    async def records(self) -> tuple[DagNodeRecord, ...]:
        return await self._connection_owner.execute(self._read_records)

    async def claim(
        self,
        task_id: TaskId,
        owner_id: BoundedLabel,
        now: datetime,
        expires_at: datetime,
    ) -> DagLease:
        return await self._connection_owner.execute(
            lambda connection: self._claim(
                connection,
                task_id,
                owner_id,
                now,
                expires_at,
            )
        )

    async def checkpoint(
        self,
        task_id: TaskId,
        owner_id: BoundedLabel,
        lease_generation: int,
        status: DagNodeStatus,
        *,
        result_sha256: Sha256 | None,
        failure_reason: BoundedLabel | None,
    ) -> DagNodeRecord:
        return await self._connection_owner.execute(
            lambda connection: self._checkpoint(
                connection,
                task_id,
                owner_id,
                lease_generation,
                status,
                result_sha256,
                failure_reason,
            )
        )

    def _initialize(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS harness_dag_graphs (
                graph_id TEXT PRIMARY KEY,
                graph_sha256 TEXT NOT NULL
            )
            """
        )
        graph_row = connection.execute(
            """
            SELECT graph_sha256 FROM harness_dag_graphs
            WHERE graph_id = ?
            """,
            (self._graph.graph_id,),
        ).fetchone()
        if (
            graph_row is not None
            and graph_row["graph_sha256"] != self._graph.graph_sha256
        ):
            raise DagStoreConflict("DAG graph hash does not match persisted graph")
        connection.execute(
            """
            INSERT OR IGNORE INTO harness_dag_graphs (graph_id, graph_sha256)
            VALUES (?, ?)
            """,
            (self._graph.graph_id, self._graph.graph_sha256),
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS harness_dag_nodes (
                graph_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                owner_id TEXT,
                lease_generation INTEGER NOT NULL,
                lease_expires_at TEXT,
                result_sha256 TEXT,
                failure_reason TEXT,
                PRIMARY KEY (graph_id, task_id)
            )
            """
        )
        for node in self._graph.nodes:
            connection.execute(
                """
                INSERT OR IGNORE INTO harness_dag_nodes
                    (graph_id, task_id, status, lease_generation)
                VALUES (?, ?, ?, 0)
                """,
                (self._graph.graph_id, node.task_id, DagNodeStatus.READY.value),
            )
        connection.commit()

    def _read_records(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[DagNodeRecord, ...]:
        rows = connection.execute(
            """
            SELECT task_id, status, owner_id, lease_generation,
                   lease_expires_at, result_sha256, failure_reason
            FROM harness_dag_nodes
            WHERE graph_id = ?
            ORDER BY task_id
            """,
            (self._graph.graph_id,),
        ).fetchall()
        return tuple(_row_to_record(row) for row in rows)

    def _claim(
        self,
        connection: sqlite3.Connection,
        task_id: TaskId,
        owner_id: BoundedLabel,
        now: datetime,
        expires_at: datetime,
    ) -> DagLease:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT status, lease_generation, lease_expires_at
            FROM harness_dag_nodes WHERE graph_id = ? AND task_id = ?
            """,
            (self._graph.graph_id, task_id),
        ).fetchone()
        if row is None:
            connection.rollback()
            raise DagStoreConflict("DAG node does not exist")
        status = str(row["status"])
        generation = int(row["lease_generation"])
        lease_expires_at = _parse_datetime(row["lease_expires_at"])
        if (
            status == DagNodeStatus.RUNNING.value
            and lease_expires_at is not None
            and lease_expires_at <= now
        ):
            status = DagNodeStatus.READY.value
            connection.execute(
                """
                UPDATE harness_dag_nodes
                SET status = ?, owner_id = NULL, lease_expires_at = NULL,
                    result_sha256 = NULL, failure_reason = NULL
                WHERE graph_id = ? AND task_id = ?
                """,
                (status, self._graph.graph_id, task_id),
            )
        if status != DagNodeStatus.READY.value:
            connection.rollback()
            raise DagStoreConflict("DAG node is not ready")
        next_generation = generation + 1
        updated = connection.execute(
            """
            UPDATE harness_dag_nodes
            SET status = ?, owner_id = ?, lease_generation = ?,
                lease_expires_at = ?, result_sha256 = NULL,
                failure_reason = NULL
            WHERE graph_id = ? AND task_id = ? AND status = ?
            """,
            (
                DagNodeStatus.RUNNING.value,
                owner_id,
                next_generation,
                expires_at.isoformat(),
                self._graph.graph_id,
                task_id,
                DagNodeStatus.READY.value,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            raise DagStoreConflict("DAG claim fence lost")
        connection.commit()
        return DagLease(
            task_id=task_id,
            owner_id=owner_id,
            lease_generation=next_generation,
            expires_at=expires_at,
        )

    def _checkpoint(
        self,
        connection: sqlite3.Connection,
        task_id: TaskId,
        owner_id: BoundedLabel,
        lease_generation: int,
        status: DagNodeStatus,
        result_sha256: Sha256 | None,
        failure_reason: BoundedLabel | None,
    ) -> DagNodeRecord:
        if status not in {DagNodeStatus.COMPLETED, DagNodeStatus.FAILED}:
            raise DagStoreConflict("DAG checkpoint status is not terminal")
        connection.execute("BEGIN IMMEDIATE")
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
                task_id,
                DagNodeStatus.RUNNING.value,
                owner_id,
                lease_generation,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            raise DagStoreConflict("DAG checkpoint fence rejected")
        connection.commit()
        row = connection.execute(
            """
            SELECT task_id, status, owner_id, lease_generation,
                   lease_expires_at, result_sha256, failure_reason
            FROM harness_dag_nodes WHERE graph_id = ? AND task_id = ?
            """,
            (self._graph.graph_id, task_id),
        ).fetchone()
        if row is None:
            raise DagStoreConflict("DAG checkpoint disappeared")
        return _row_to_record(row)


def _row_to_record(row: sqlite3.Row) -> DagNodeRecord:
    return DagNodeRecord(
        task_id=str(row["task_id"]),
        status=DagNodeStatus(str(row["status"])),
        owner_id=row["owner_id"],
        lease_generation=int(row["lease_generation"]),
        lease_expires_at=_parse_datetime(row["lease_expires_at"]),
        result_sha256=row["result_sha256"],
        failure_reason=row["failure_reason"],
    )


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DagStoreConflict("DAG lease timestamp is invalid")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise DagStoreConflict("DAG lease timestamp is invalid") from error
