"""Explicit local SQLite schema for immutable journal facts."""

SQLITE_SCHEMA_VERSION = 1

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_journal_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1)
);

INSERT OR IGNORE INTO harness_journal_schema (singleton, schema_version)
VALUES (1, 1);

CREATE TABLE IF NOT EXISTS harness_aggregates (
    workspace_id TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    current_sequence INTEGER NOT NULL
        CHECK (current_sequence >= 0 AND current_sequence <= 9223372036854775807),
    PRIMARY KEY (workspace_id, aggregate_id)
);

CREATE TABLE IF NOT EXISTS harness_events (
    journal_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_sequence INTEGER NOT NULL CHECK (aggregate_sequence >= 1),
    event_json TEXT NOT NULL,
    event_sha256 TEXT NOT NULL CHECK (length(event_sha256) = 64),
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    durability TEXT NOT NULL CHECK (durability IN ('synchronous', 'buffered')),
    committed_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id, aggregate_id)
        REFERENCES harness_aggregates(workspace_id, aggregate_id),
    UNIQUE (workspace_id, event_id),
    UNIQUE (workspace_id, aggregate_id, aggregate_sequence)
);

CREATE INDEX IF NOT EXISTS ix_harness_events_aggregate_sequence
ON harness_events (workspace_id, aggregate_id, aggregate_sequence);

CREATE INDEX IF NOT EXISTS ix_harness_events_workspace_journal_sequence
ON harness_events (workspace_id, journal_sequence);

CREATE TABLE IF NOT EXISTS harness_idempotency (
    workspace_id TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    result_json TEXT NOT NULL,
    PRIMARY KEY (workspace_id, aggregate_id, idempotency_key),
    FOREIGN KEY (workspace_id, aggregate_id)
        REFERENCES harness_aggregates(workspace_id, aggregate_id)
);

CREATE TRIGGER IF NOT EXISTS harness_events_no_update
BEFORE UPDATE ON harness_events
BEGIN
    SELECT RAISE(ABORT, 'harness events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_events_no_delete
BEFORE DELETE ON harness_events
BEGIN
    SELECT RAISE(ABORT, 'harness events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_idempotency_no_update
BEFORE UPDATE ON harness_idempotency
BEGIN
    SELECT RAISE(ABORT, 'harness idempotency receipts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_idempotency_no_delete
BEFORE DELETE ON harness_idempotency
BEGIN
    SELECT RAISE(ABORT, 'harness idempotency receipts are immutable');
END;
"""
