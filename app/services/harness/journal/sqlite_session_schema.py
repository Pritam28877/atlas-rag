"""SQLite schema for durable command replay and subscription cursors."""

SQLITE_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_command_receipts (
    workspace_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    command_kind TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_kind TEXT NOT NULL,
    result_json TEXT NOT NULL,
    result_sha256 TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, idempotency_key),
    CHECK (
        length(workspace_id) = 36
        AND substr(workspace_id, 1, 4) = 'wsp_'
        AND substr(workspace_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(principal_id) = 36
        AND substr(principal_id, 1, 4) = 'prn_'
        AND substr(principal_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(idempotency_key) BETWEEN 16 AND 128
        AND substr(idempotency_key, 1, 1) GLOB '[A-Za-z0-9]'
        AND idempotency_key NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
    CHECK (
        length(command_kind) BETWEEN 3 AND 64
        AND substr(command_kind, 1, 1) GLOB '[a-z]'
        AND command_kind NOT GLOB '*[^a-z0-9.]*'
    ),
    CHECK (
        length(request_sha256) = 64
        AND request_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(response_kind) BETWEEN 3 AND 64
        AND substr(response_kind, 1, 1) GLOB '[a-z]'
        AND response_kind NOT GLOB '*[^a-z0-9_]*'
    ),
    CHECK (
        length(CAST(result_json AS BLOB)) <= 262144
        AND json_valid(result_json)
        AND json_type(result_json) = 'object'
    ),
    CHECK (
        length(result_sha256) = 64
        AND result_sha256 NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE INDEX IF NOT EXISTS ix_harness_command_receipts_committed
ON harness_command_receipts (
    workspace_id, principal_id, committed_at, idempotency_key
);

CREATE TABLE IF NOT EXISTS harness_subscription_cursors (
    workspace_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    subscription_id TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1 CHECK (generation >= 1),
    acknowledged_sequence INTEGER NOT NULL
        CHECK (
            acknowledged_sequence >= 0
            AND acknowledged_sequence <= 9223372036854775807
        ),
    delivered_sequence INTEGER NOT NULL
        CHECK (
            delivered_sequence >= 0
            AND delivered_sequence <= 9223372036854775807
        ),
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, subscription_id),
    CHECK (
        length(workspace_id) = 36
        AND substr(workspace_id, 1, 4) = 'wsp_'
        AND substr(workspace_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(principal_id) = 36
        AND substr(principal_id, 1, 4) = 'prn_'
        AND substr(principal_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(subscription_id) = 36
        AND substr(subscription_id, 1, 4) = 'sub_'
        AND substr(subscription_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (acknowledged_sequence <= delivered_sequence),
    CHECK (expires_at >= updated_at)
);

CREATE INDEX IF NOT EXISTS ix_harness_subscription_cursors_expiry
ON harness_subscription_cursors (
    expires_at, workspace_id, principal_id, subscription_id
);

CREATE TRIGGER IF NOT EXISTS harness_command_receipts_no_update
BEFORE UPDATE ON harness_command_receipts
BEGIN
    SELECT RAISE(ABORT, 'command receipts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_command_receipts_no_delete
BEFORE DELETE ON harness_command_receipts
BEGIN
    SELECT RAISE(ABORT, 'command receipts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_subscription_cursor_transition
BEFORE UPDATE ON harness_subscription_cursors
WHEN NEW.workspace_id <> OLD.workspace_id
    OR NEW.principal_id <> OLD.principal_id
    OR NEW.subscription_id <> OLD.subscription_id
    OR NEW.generation <> OLD.generation + 1
    OR NEW.acknowledged_sequence < OLD.acknowledged_sequence
    OR NEW.delivered_sequence < OLD.delivered_sequence
    OR NEW.updated_at < OLD.updated_at
    OR NEW.expires_at < OLD.expires_at
BEGIN
    SELECT RAISE(ABORT, 'subscription cursor transition is invalid');
END;
"""
