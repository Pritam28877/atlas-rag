"""Atomic persistence operations for durable background jobs."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Never

from app.services.harness.journal.errors import (
    BackgroundJobStoreConflict,
    BackgroundJobStoreConflictCode,
    JournalStorageError,
)
from app.services.harness.journal.sqlite_background_job_rows import (
    count_workspace_background_jobs,
    decode_background_job,
    load_background_job_row,
)
from app.services.harness.journal.sqlite_recovery_rows import decode_operation
from app.services.harness.protocol import OperationState
from app.services.harness.protocol.background_jobs import (
    BackgroundJobRecord,
    BackgroundJobState,
    require_background_job_transition,
)


def save_background_job(
    connection: sqlite3.Connection,
    job: BackgroundJobRecord,
    *,
    maximum_jobs_per_workspace: int,
) -> BackgroundJobRecord:
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing_row = load_background_job_row(
            connection,
            job.workspace_id,
            job.operation_id,
        )
        if existing_row is None:
            _insert_background_job(
                connection,
                job,
                maximum_jobs_per_workspace=maximum_jobs_per_workspace,
            )
        else:
            existing = decode_background_job(existing_row)
            if existing == job:
                connection.commit()
                return existing
            _validate_transition(existing, job)
            _update_background_job(connection, job)
        stored_row = load_background_job_row(
            connection,
            job.workspace_id,
            job.operation_id,
        )
        if stored_row is None:
            raise JournalStorageError("background job was not stored")
        stored = decode_background_job(stored_row)
        if stored != job:
            raise JournalStorageError("stored background job differs")
        connection.commit()
        return stored
    except BackgroundJobStoreConflict:
        connection.rollback()
        raise
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise BackgroundJobStoreConflict(
            BackgroundJobStoreConflictCode.IDENTITY
        ) from error
    except BaseException:
        connection.rollback()
        raise


def purge_expired_background_jobs(
    connection: sqlite3.Connection,
    *,
    expired_at_or_before: datetime,
    maximum_records: int,
) -> int:
    connection.execute("BEGIN IMMEDIATE")
    try:
        deleted = connection.execute(
            """
            DELETE FROM harness_background_jobs
            WHERE rowid IN (
                SELECT rowid FROM harness_background_jobs
                WHERE job_state IN (
                    'completed', 'failed', 'cancelled', 'ambiguous'
                )
                  AND retention_expires_at <= ?
                ORDER BY retention_expires_at, workspace_id, operation_id
                LIMIT ?
            )
            """,
            (expired_at_or_before.isoformat(), maximum_records),
        )
        connection.commit()
        return deleted.rowcount
    except BaseException:
        connection.rollback()
        raise


def delete_expired_background_job(
    connection: sqlite3.Connection,
    job: BackgroundJobRecord,
    *,
    expired_at_or_before: datetime,
) -> bool:
    connection.execute("BEGIN IMMEDIATE")
    try:
        deleted = connection.execute(
            """
            DELETE FROM harness_background_jobs
            WHERE workspace_id = ? AND operation_id = ?
              AND job_state IN (
                  'completed', 'failed', 'cancelled', 'ambiguous'
              )
              AND retention_expires_at <= ?
              AND record_json = ?
            """,
            (
                job.workspace_id,
                job.operation_id,
                expired_at_or_before.isoformat(),
                job.model_dump_json(),
            ),
        )
        connection.commit()
        return deleted.rowcount == 1
    except BaseException:
        connection.rollback()
        raise


def _insert_background_job(
    connection: sqlite3.Connection,
    job: BackgroundJobRecord,
    *,
    maximum_jobs_per_workspace: int,
) -> None:
    if (
        job.state is not BackgroundJobState.QUEUED
        or job.execution_generation != 0
    ):
        _reject(BackgroundJobStoreConflictCode.STATE)
    _require_operation_binding(connection, job)
    if (
        count_workspace_background_jobs(connection, job.workspace_id)
        >= maximum_jobs_per_workspace
    ):
        _reject(BackgroundJobStoreConflictCode.CAPACITY)
    connection.execute(
        """
        INSERT INTO harness_background_jobs (
            workspace_id, operation_id, owner_principal_id, job_state,
            execution_generation, execution_owner_id, record_json,
            updated_at, retention_expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        _job_values(job),
    )


def _require_operation_binding(
    connection: sqlite3.Connection,
    job: BackgroundJobRecord,
) -> None:
    row = connection.execute(
        """
        SELECT operation_id, operation_state, idempotency_class, operation_json
        FROM harness_recovery_operations
        WHERE workspace_id = ? AND operation_id = ?
        """,
        (job.workspace_id, job.operation_id),
    ).fetchone()
    if row is None:
        _reject(BackgroundJobStoreConflictCode.IDENTITY)
    operation = decode_operation(row)
    if (
        operation.state is not OperationState.DISPATCHED
        or operation.tool_name != job.tool_name
        or operation.tool_version != job.tool_version
        or operation.args_sha256 != job.args_sha256
        or operation.capability != job.capability
        or operation.idempotency_class is not job.idempotency_class
    ):
        _reject(BackgroundJobStoreConflictCode.IDENTITY)


def _update_background_job(
    connection: sqlite3.Connection,
    job: BackgroundJobRecord,
) -> None:
    updated = connection.execute(
        """
        UPDATE harness_background_jobs
        SET job_state = ?, execution_generation = ?,
            execution_owner_id = ?, record_json = ?, updated_at = ?,
            retention_expires_at = ?
        WHERE workspace_id = ? AND operation_id = ?
        """,
        (
            job.state.value,
            job.execution_generation,
            job.execution_owner_id,
            job.model_dump_json(),
            job.updated_at.isoformat(),
            _optional_timestamp(job.retention_expires_at),
            job.workspace_id,
            job.operation_id,
        ),
    )
    if updated.rowcount != 1:
        _reject(BackgroundJobStoreConflictCode.STATE)


def _validate_transition(
    existing: BackgroundJobRecord,
    requested: BackgroundJobRecord,
) -> None:
    if existing.owner_principal_id != requested.owner_principal_id:
        _reject(BackgroundJobStoreConflictCode.OWNER)
    immutable_identity = (
        "call_id",
        "requested_name",
        "tool_name",
        "tool_version",
        "capability",
        "idempotency_class",
        "descriptor_sha256",
        "arguments_json",
        "args_sha256",
        "created_at",
        "queue_expires_at",
    )
    if any(
        getattr(existing, field_name) != getattr(requested, field_name)
        for field_name in immutable_identity
    ):
        _reject(BackgroundJobStoreConflictCode.IDENTITY)
    try:
        require_background_job_transition(existing.state, requested.state)
    except ValueError:
        _reject(BackgroundJobStoreConflictCode.STATE)
    if requested.updated_at < existing.updated_at:
        _reject(BackgroundJobStoreConflictCode.STATE)
    _validate_fencing(existing, requested)


def _validate_fencing(
    existing: BackgroundJobRecord,
    requested: BackgroundJobRecord,
) -> None:
    if existing.state is BackgroundJobState.QUEUED:
        expected_generation = (
            existing.execution_generation + 1
            if requested.state is BackgroundJobState.RUNNING
            else existing.execution_generation
        )
        if requested.execution_generation != expected_generation:
            _reject(BackgroundJobStoreConflictCode.FENCING)
        return
    if requested.execution_generation != existing.execution_generation:
        _reject(BackgroundJobStoreConflictCode.FENCING)
    remains_active = requested.state in {
        BackgroundJobState.RUNNING,
        BackgroundJobState.CANCELLING,
    }
    if remains_active and (
        requested.execution_owner_id != existing.execution_owner_id
    ):
        _reject(BackgroundJobStoreConflictCode.FENCING)
    existing_expiry = existing.execution_lease_expires_at
    requested_expiry = requested.execution_lease_expires_at
    if (
        remains_active
        and (
            existing_expiry is None
            or requested_expiry is None
            or requested_expiry < existing_expiry
        )
    ):
        _reject(BackgroundJobStoreConflictCode.FENCING)
    if (
        requested.state is existing.state
        and (
            requested_expiry is None
            or existing_expiry is None
            or requested_expiry <= existing_expiry
        )
    ):
        _reject(BackgroundJobStoreConflictCode.FENCING)


def _job_values(job: BackgroundJobRecord) -> tuple[object, ...]:
    return (
        job.workspace_id,
        job.operation_id,
        job.owner_principal_id,
        job.state.value,
        job.execution_generation,
        job.execution_owner_id,
        job.model_dump_json(),
        job.updated_at.isoformat(),
        _optional_timestamp(job.retention_expires_at),
    )


def _optional_timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _reject(code: BackgroundJobStoreConflictCode) -> Never:
    raise BackgroundJobStoreConflict(code)
