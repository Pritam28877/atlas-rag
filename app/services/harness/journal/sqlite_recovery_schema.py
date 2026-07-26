"""SQLite schema for durable operation and fencing-lease recovery facts."""

SQLITE_RECOVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_recovery_operations (
    workspace_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    operation_state TEXT NOT NULL CHECK (
        operation_state IN (
            'prepared', 'dispatched', 'completed',
            'failed', 'ambiguous', 'cancelled'
        )
    ),
    idempotency_class TEXT NOT NULL CHECK (
        idempotency_class IN ('read_only', 'repeatable', 'non_idempotent')
    ),
    operation_json TEXT NOT NULL CHECK (
        length(CAST(operation_json AS BLOB)) <= 65536
        AND json_valid(operation_json)
        AND json_type(operation_json) = 'object'
        AND json_extract(operation_json, '$.operation_id') = operation_id
        AND json_extract(operation_json, '$.state') = operation_state
        AND json_extract(operation_json, '$.idempotency_class')
            = idempotency_class
    ),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, operation_id),
    CHECK (
        length(workspace_id) = 36
        AND substr(workspace_id, 1, 4) = 'wsp_'
        AND substr(workspace_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(operation_id) = 36
        AND substr(operation_id, 1, 4) = 'opn_'
        AND substr(operation_id, 5) NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE INDEX IF NOT EXISTS ix_harness_recovery_active_operations
ON harness_recovery_operations (
    workspace_id, operation_state, updated_at, operation_id
) WHERE operation_state IN ('prepared', 'dispatched', 'ambiguous');

CREATE TABLE IF NOT EXISTS harness_recovery_leases (
    workspace_id TEXT NOT NULL,
    lease_sha256 TEXT NOT NULL,
    fencing_generation INTEGER NOT NULL
        CHECK (fencing_generation >= 1),
    expires_at TEXT NOT NULL,
    lease_state TEXT NOT NULL CHECK (lease_state IN ('active', 'expired')),
    expired_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, lease_sha256),
    CHECK (
        length(lease_sha256) = 64
        AND lease_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        (lease_state = 'active' AND expired_at IS NULL)
        OR (
            lease_state = 'expired'
            AND expired_at IS NOT NULL
            AND expired_at >= expires_at
        )
    ),
    CHECK (updated_at >= expires_at OR lease_state = 'active')
);

CREATE INDEX IF NOT EXISTS ix_harness_recovery_active_leases
ON harness_recovery_leases (workspace_id, expires_at, lease_sha256)
WHERE lease_state = 'active';

CREATE TRIGGER IF NOT EXISTS harness_recovery_operation_transition
BEFORE UPDATE ON harness_recovery_operations
WHEN NEW.workspace_id <> OLD.workspace_id
    OR NEW.operation_id <> OLD.operation_id
    OR NEW.idempotency_class <> OLD.idempotency_class
    OR NEW.updated_at < OLD.updated_at
    OR NOT (
        (
            OLD.operation_state = 'prepared'
            AND NEW.operation_state IN ('dispatched', 'cancelled')
        )
        OR (
            OLD.operation_state = 'dispatched'
            AND NEW.operation_state IN (
                'completed', 'failed', 'ambiguous', 'cancelled'
            )
        )
        OR (
            OLD.operation_state = 'ambiguous'
            AND NEW.operation_state IN ('completed', 'failed')
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'recovery operation transition is invalid');
END;

CREATE TRIGGER IF NOT EXISTS harness_recovery_operations_no_delete
BEFORE DELETE ON harness_recovery_operations
BEGIN
    SELECT RAISE(ABORT, 'recovery operation evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_recovery_lease_transition
BEFORE UPDATE ON harness_recovery_leases
WHEN NEW.workspace_id <> OLD.workspace_id
    OR NEW.lease_sha256 <> OLD.lease_sha256
    OR NEW.fencing_generation <> OLD.fencing_generation
    OR NEW.expires_at <> OLD.expires_at
    OR NEW.updated_at < OLD.updated_at
    OR OLD.lease_state <> 'active'
    OR NEW.lease_state <> 'expired'
BEGIN
    SELECT RAISE(ABORT, 'recovery lease transition is invalid');
END;

CREATE TRIGGER IF NOT EXISTS harness_recovery_leases_no_delete
BEFORE DELETE ON harness_recovery_leases
BEGIN
    SELECT RAISE(ABORT, 'recovery lease evidence is immutable');
END;
"""
