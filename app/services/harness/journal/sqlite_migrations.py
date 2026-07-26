"""Forward-only local SQLite journal schema migrations."""

SQLITE_MIGRATE_V1_TO_V2 = """
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS harness_events_no_update;
DROP TRIGGER IF EXISTS harness_events_no_delete;
DROP TRIGGER IF EXISTS harness_events_require_health;
DROP TRIGGER IF EXISTS harness_events_require_healthy_health;

ALTER TABLE harness_events RENAME TO harness_events_v1;

CREATE TABLE harness_journal_positions (
    workspace_id TEXT PRIMARY KEY,
    current_sequence INTEGER NOT NULL
        CHECK (current_sequence >= 0 AND current_sequence <= 9223372036854775807)
);

CREATE TABLE harness_events (
    journal_sequence INTEGER NOT NULL CHECK (journal_sequence >= 1),
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
    PRIMARY KEY (workspace_id, journal_sequence),
    UNIQUE (workspace_id, event_id),
    UNIQUE (workspace_id, aggregate_id, aggregate_sequence)
);

INSERT INTO harness_events (
    journal_sequence, event_id, workspace_id, aggregate_id, aggregate_sequence,
    event_json, event_sha256, request_sha256, durability, committed_at
)
SELECT
    journal_sequence, event_id, workspace_id, aggregate_id, aggregate_sequence,
    event_json, event_sha256, request_sha256, durability, committed_at
FROM harness_events_v1;

INSERT INTO harness_journal_positions (workspace_id, current_sequence)
SELECT workspace_id, MAX(journal_sequence)
FROM harness_events
GROUP BY workspace_id;

DROP TABLE harness_events_v1;
UPDATE harness_journal_schema SET schema_version = 2 WHERE singleton = 1;

COMMIT;
PRAGMA foreign_keys = ON;
"""
