"""SQLite schema for durable hash-bound approval state and receipts."""

SQLITE_APPROVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_approval_records (
    workspace_id TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    authorization_sha256 TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation BETWEEN 1 AND 16),
    approval_state TEXT NOT NULL CHECK (
        approval_state IN (
            'requested', 'approved', 'denied', 'expired',
            'revoked', 'cancelled', 'consumed'
        )
    ),
    record_json TEXT NOT NULL CHECK (
        length(CAST(record_json AS BLOB)) <= 65536
        AND json_valid(record_json)
        AND json_type(record_json) = 'object'
    ),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, approval_id),
    CHECK (
        length(workspace_id) = 36
        AND substr(workspace_id, 1, 4) = 'wsp_'
        AND substr(workspace_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(approval_id) = 36
        AND substr(approval_id, 1, 4) = 'apr_'
        AND substr(approval_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(principal_id) = 36
        AND substr(principal_id, 1, 4) = 'prn_'
        AND substr(principal_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(grant_id) = 36
        AND substr(grant_id, 1, 4) = 'grt_'
        AND substr(grant_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(authorization_sha256) = 64
        AND authorization_sha256 NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE INDEX IF NOT EXISTS ix_harness_approval_pending_expiry
ON harness_approval_records (
    workspace_id, approval_state, expires_at, approval_id
);

CREATE TABLE IF NOT EXISTS harness_approval_receipts (
    workspace_id TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation BETWEEN 1 AND 16),
    receipt_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL CHECK (
        length(CAST(receipt_json AS BLOB)) <= 16384
        AND json_valid(receipt_json)
        AND json_type(receipt_json) = 'object'
    ),
    transitioned_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, approval_id, generation),
    FOREIGN KEY (workspace_id, approval_id)
        REFERENCES harness_approval_records(workspace_id, approval_id),
    CHECK (
        length(receipt_sha256) = 64
        AND receipt_sha256 NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE TRIGGER IF NOT EXISTS harness_approval_record_transition
BEFORE UPDATE ON harness_approval_records
WHEN NEW.workspace_id <> OLD.workspace_id
    OR NEW.approval_id <> OLD.approval_id
    OR NEW.principal_id <> OLD.principal_id
    OR NEW.grant_id <> OLD.grant_id
    OR NEW.authorization_sha256 <> OLD.authorization_sha256
    OR NEW.expires_at <> OLD.expires_at
    OR NEW.generation <> OLD.generation + 1
    OR NEW.updated_at < OLD.updated_at
    OR NOT (
        (OLD.approval_state = 'requested'
            AND NEW.approval_state IN (
                'approved', 'denied', 'expired', 'cancelled'
            ))
        OR (OLD.approval_state = 'approved'
            AND NEW.approval_state IN ('consumed', 'revoked', 'expired'))
    )
BEGIN
    SELECT RAISE(ABORT, 'approval record transition is invalid');
END;

CREATE TRIGGER IF NOT EXISTS harness_approval_record_no_delete
BEFORE DELETE ON harness_approval_records
BEGIN
    SELECT RAISE(ABORT, 'approval records are immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_approval_receipt_no_update
BEFORE UPDATE ON harness_approval_receipts
BEGIN
    SELECT RAISE(ABORT, 'approval receipts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_approval_receipt_no_delete
BEFORE DELETE ON harness_approval_receipts
BEGIN
    SELECT RAISE(ABORT, 'approval receipts are immutable');
END;
"""
