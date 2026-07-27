"""SQLite schema for bounded durable background tool jobs."""

SQLITE_BACKGROUND_JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_background_jobs (
    workspace_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    owner_principal_id TEXT NOT NULL,
    job_state TEXT NOT NULL CHECK (
        job_state IN (
            'queued', 'running', 'cancelling', 'completed',
            'failed', 'cancelled', 'ambiguous'
        )
    ),
    execution_generation INTEGER NOT NULL CHECK (
        execution_generation BETWEEN 0 AND 2147483647
    ),
    execution_owner_id TEXT,
    record_json TEXT NOT NULL CHECK (
        length(CAST(record_json AS BLOB)) <= 262144
        AND json_valid(record_json)
        AND json_type(record_json) = 'object'
        AND json_extract(record_json, '$.workspace_id') = workspace_id
        AND json_extract(record_json, '$.operation_id') = operation_id
        AND json_extract(record_json, '$.owner_principal_id') = owner_principal_id
        AND json_extract(record_json, '$.state') = job_state
        AND json_extract(record_json, '$.execution_generation')
            = execution_generation
        AND json_extract(record_json, '$.execution_owner_id')
            IS execution_owner_id
    ),
    updated_at TEXT NOT NULL,
    retention_expires_at TEXT,
    PRIMARY KEY (workspace_id, operation_id),
    FOREIGN KEY (workspace_id, operation_id)
        REFERENCES harness_recovery_operations(workspace_id, operation_id),
    CHECK (
        length(workspace_id) = 36
        AND substr(workspace_id, 1, 4) = 'wsp_'
        AND substr(workspace_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(operation_id) = 36
        AND substr(operation_id, 1, 4) = 'opn_'
        AND substr(operation_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(owner_principal_id) = 36
        AND substr(owner_principal_id, 1, 4) = 'prn_'
        AND substr(owner_principal_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        (
            job_state IN ('running', 'cancelling')
            AND length(execution_owner_id) = 36
            AND substr(execution_owner_id, 1, 4) = 'jow_'
            AND substr(execution_owner_id, 5) NOT GLOB '*[^0-9a-f]*'
        ) OR (
            job_state NOT IN ('running', 'cancelling')
            AND execution_owner_id IS NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS ix_harness_background_jobs_owner
ON harness_background_jobs (
    workspace_id, owner_principal_id, updated_at, operation_id
);

CREATE INDEX IF NOT EXISTS ix_harness_background_jobs_recovery
ON harness_background_jobs (
    job_state, updated_at, workspace_id, operation_id
) WHERE job_state IN ('queued', 'running', 'cancelling');

CREATE INDEX IF NOT EXISTS ix_harness_background_jobs_retention
ON harness_background_jobs (
    retention_expires_at, workspace_id, operation_id
) WHERE retention_expires_at IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS harness_background_job_transition
BEFORE UPDATE ON harness_background_jobs
WHEN NEW.workspace_id <> OLD.workspace_id
    OR NEW.operation_id <> OLD.operation_id
    OR NEW.owner_principal_id <> OLD.owner_principal_id
    OR NEW.updated_at < OLD.updated_at
    OR NEW.execution_generation < OLD.execution_generation
    OR (
        OLD.job_state = 'queued'
        AND NEW.job_state = 'running'
        AND NEW.execution_generation <> OLD.execution_generation + 1
    )
    OR (
        NOT (OLD.job_state = 'queued' AND NEW.job_state = 'running')
        AND NEW.execution_generation <> OLD.execution_generation
    )
    OR (
        OLD.job_state IN ('running', 'cancelling')
        AND NEW.job_state IN ('running', 'cancelling')
        AND NEW.execution_owner_id IS NOT OLD.execution_owner_id
    )
    OR NOT (
        (OLD.job_state = 'queued' AND NEW.job_state IN (
            'running', 'failed', 'cancelled'
        ))
        OR (OLD.job_state = 'running' AND NEW.job_state IN (
            'running', 'cancelling', 'completed', 'failed', 'ambiguous'
        ))
        OR (OLD.job_state = 'cancelling' AND NEW.job_state IN (
            'cancelling', 'cancelled', 'failed', 'ambiguous'
        ))
    )
BEGIN
    SELECT RAISE(ABORT, 'background job transition is invalid');
END;

CREATE TRIGGER IF NOT EXISTS harness_background_job_delete_terminal_only
BEFORE DELETE ON harness_background_jobs
WHEN OLD.job_state NOT IN ('completed', 'failed', 'cancelled', 'ambiguous')
BEGIN
    SELECT RAISE(ABORT, 'active background job cannot be deleted');
END;
"""
