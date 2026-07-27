"""Strict decoding and bounded reads for durable background jobs."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from pydantic import ValidationError

from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.protocol.background_jobs import (
    MAXIMUM_BACKGROUND_JOBS,
    BackgroundJobRecord,
)


def decode_background_job(row: sqlite3.Row) -> BackgroundJobRecord:
    try:
        job = BackgroundJobRecord.model_validate_json(row["record_json"])
    except (TypeError, ValueError, ValidationError) as error:
        raise JournalStorageError(
            "background job evidence is invalid"
        ) from error
    if (
        job.workspace_id != row["workspace_id"]
        or job.operation_id != row["operation_id"]
        or job.owner_principal_id != row["owner_principal_id"]
        or job.state.value != row["job_state"]
        or job.execution_generation != row["execution_generation"]
        or job.execution_owner_id != row["execution_owner_id"]
        or job.updated_at.isoformat() != row["updated_at"]
        or _optional_timestamp(job.retention_expires_at)
        != row["retention_expires_at"]
    ):
        raise JournalStorageError("background job columns disagree")
    return job


def load_background_job_row(
    connection: sqlite3.Connection,
    workspace_id: str,
    operation_id: str,
) -> sqlite3.Row | None:
    row: sqlite3.Row | None = connection.execute(
        """
        SELECT workspace_id, operation_id, owner_principal_id, job_state,
               execution_generation, execution_owner_id, record_json,
               updated_at, retention_expires_at
        FROM harness_background_jobs
        WHERE workspace_id = ? AND operation_id = ?
        """,
        (workspace_id, operation_id),
    ).fetchone()
    return row


def load_owned_background_jobs(
    connection: sqlite3.Connection,
    workspace_id: str,
    owner_principal_id: str,
    *,
    maximum_records: int,
) -> tuple[BackgroundJobRecord, ...]:
    _validate_limit(maximum_records)
    rows = connection.execute(
        """
        SELECT workspace_id, operation_id, owner_principal_id, job_state,
               execution_generation, execution_owner_id, record_json,
               updated_at, retention_expires_at
        FROM harness_background_jobs
        WHERE workspace_id = ? AND owner_principal_id = ?
        ORDER BY updated_at DESC, operation_id DESC
        LIMIT ?
        """,
        (workspace_id, owner_principal_id, maximum_records),
    ).fetchall()
    return tuple(decode_background_job(row) for row in rows)


def load_recoverable_background_jobs(
    connection: sqlite3.Connection,
    *,
    maximum_records: int,
) -> tuple[BackgroundJobRecord, ...]:
    _validate_limit(maximum_records)
    rows = connection.execute(
        """
        SELECT workspace_id, operation_id, owner_principal_id, job_state,
               execution_generation, execution_owner_id, record_json,
               updated_at, retention_expires_at
        FROM harness_background_jobs
        WHERE job_state IN ('queued', 'running', 'cancelling')
        ORDER BY updated_at, workspace_id, operation_id
        LIMIT ?
        """,
        (maximum_records + 1,),
    ).fetchall()
    if len(rows) > maximum_records:
        raise JournalStorageError("background job recovery bound exceeded")
    return tuple(decode_background_job(row) for row in rows)


def load_expired_background_jobs(
    connection: sqlite3.Connection,
    *,
    expired_at_or_before: datetime,
    maximum_records: int,
) -> tuple[BackgroundJobRecord, ...]:
    _validate_limit(maximum_records)
    rows = connection.execute(
        """
        SELECT workspace_id, operation_id, owner_principal_id, job_state,
               execution_generation, execution_owner_id, record_json,
               updated_at, retention_expires_at
        FROM harness_background_jobs
        WHERE job_state IN ('completed', 'failed', 'cancelled', 'ambiguous')
          AND retention_expires_at <= ?
        ORDER BY retention_expires_at, workspace_id, operation_id
        LIMIT ?
        """,
        (expired_at_or_before.isoformat(), maximum_records),
    ).fetchall()
    return tuple(decode_background_job(row) for row in rows)


def count_workspace_background_jobs(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS job_count
        FROM harness_background_jobs
        WHERE workspace_id = ?
        """,
        (workspace_id,),
    ).fetchone()
    if row is None or not isinstance(row["job_count"], int):
        raise JournalStorageError("background job count is invalid")
    return row["job_count"]


def _validate_limit(maximum_records: int) -> None:
    if not 1 <= maximum_records <= MAXIMUM_BACKGROUND_JOBS:
        raise ValueError("background job limit must be between 1 and 4096")


def _optional_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
