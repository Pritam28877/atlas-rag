"""Explicit local SQLite schema for immutable journal facts."""

from app.services.harness.journal.sqlite_approval_schema import (
    SQLITE_APPROVAL_SCHEMA,
)
from app.services.harness.journal.sqlite_operation_schema import (
    SQLITE_OPERATION_ADMISSION_SCHEMA,
)
from app.services.harness.journal.sqlite_provider_cost_schema import (
    SQLITE_PROVIDER_COST_SCHEMA,
)
from app.services.harness.journal.sqlite_recovery_schema import (
    SQLITE_RECOVERY_SCHEMA,
)
from app.services.harness.journal.sqlite_retention_evidence_schema import (
    SQLITE_RETENTION_EVIDENCE_SCHEMA,
)
from app.services.harness.journal.sqlite_retention_schema import (
    SQLITE_RETENTION_SCHEMA,
)
from app.services.harness.journal.sqlite_session_schema import (
    SQLITE_SESSION_SCHEMA,
)
from app.services.harness.journal.sqlite_storage_schema import (
    SQLITE_STORAGE_SCHEMA,
)

SQLITE_SCHEMA_VERSION = 10

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_journal_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1)
);

INSERT OR IGNORE INTO harness_journal_schema (singleton, schema_version)
VALUES (1, 10);

CREATE TABLE IF NOT EXISTS harness_journal_positions (
    workspace_id TEXT PRIMARY KEY,
    current_sequence INTEGER NOT NULL
        CHECK (current_sequence >= 0 AND current_sequence <= 9223372036854775807)
);

CREATE TABLE IF NOT EXISTS harness_aggregates (
    workspace_id TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    current_sequence INTEGER NOT NULL
        CHECK (current_sequence >= 0 AND current_sequence <= 9223372036854775807),
    PRIMARY KEY (workspace_id, aggregate_id)
);

CREATE TABLE IF NOT EXISTS harness_events (
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

CREATE TABLE IF NOT EXISTS harness_projection_checkpoints (
    workspace_id TEXT NOT NULL,
    projection_name TEXT NOT NULL,
    projection_version TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0),
    last_journal_sequence INTEGER NOT NULL DEFAULT 0
        CHECK (last_journal_sequence >= 0),
    event_count INTEGER NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    state_json TEXT NOT NULL CHECK (
        length(CAST(state_json AS BLOB)) <= 4194304
        AND json_valid(state_json)
        AND json_type(state_json) = 'object'
    ),
    state_sha256 TEXT NOT NULL CHECK (
        length(state_sha256) = 64
        AND state_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    projection_status TEXT NOT NULL DEFAULT 'healthy',
    failure_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, projection_name),
    CHECK (
        length(workspace_id) = 36
        AND substr(workspace_id, 1, 4) = 'wsp_'
        AND substr(workspace_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(projection_name) BETWEEN 3 AND 128
        AND substr(projection_name, 1, 1) GLOB '[a-z]'
        AND projection_name NOT GLOB '*[^a-z0-9._-]*'
    ),
    CHECK (
        projection_version GLOB '1.[0-9]'
        OR projection_version GLOB '1.[0-9][0-9]'
        OR projection_version GLOB '1.[0-9][0-9][0-9]'
    ),
    CHECK (
        (
            projection_status = 'healthy'
            AND failure_code IS NULL
        ) OR (
            projection_status IN ('diverged', 'needs_operator')
            AND length(failure_code) BETWEEN 3 AND 128
            AND substr(failure_code, 1, 1) GLOB '[a-z]'
            AND failure_code NOT GLOB '*[^a-z0-9_]*'
        )
    )
);

CREATE INDEX IF NOT EXISTS ix_harness_projection_unhealthy
ON harness_projection_checkpoints (
    projection_status, updated_at, workspace_id, projection_name
) WHERE projection_status <> 'healthy';

CREATE TABLE IF NOT EXISTS harness_journal_health (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0),
    journal_status TEXT NOT NULL DEFAULT 'healthy',
    failure_code TEXT,
    verified_event_count INTEGER NOT NULL DEFAULT 0
        CHECK (verified_event_count >= 0),
    verified_at TEXT NOT NULL,
    CHECK (
        (
            journal_status = 'healthy'
            AND failure_code IS NULL
        ) OR (
            journal_status = 'needs_operator'
            AND length(failure_code) BETWEEN 3 AND 128
            AND substr(failure_code, 1, 1) GLOB '[a-z]'
            AND failure_code NOT GLOB '*[^a-z0-9_]*'
        )
    )
);

INSERT OR IGNORE INTO harness_journal_health (
    singleton, verified_at
) VALUES (1, '1970-01-01T00:00:00+00:00');

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

CREATE TRIGGER IF NOT EXISTS harness_aggregates_monotonic
BEFORE UPDATE ON harness_aggregates
WHEN NEW.workspace_id <> OLD.workspace_id
    OR NEW.aggregate_id <> OLD.aggregate_id
    OR NEW.current_sequence < OLD.current_sequence
BEGIN
    SELECT RAISE(ABORT, 'journal aggregate is monotonic');
END;

CREATE TRIGGER IF NOT EXISTS harness_aggregates_no_delete
BEFORE DELETE ON harness_aggregates
BEGIN
    SELECT RAISE(ABORT, 'journal aggregate is monotonic');
END;

CREATE TRIGGER IF NOT EXISTS harness_journal_positions_monotonic
BEFORE UPDATE ON harness_journal_positions
WHEN NEW.workspace_id <> OLD.workspace_id
    OR NEW.current_sequence < OLD.current_sequence
BEGIN
    SELECT RAISE(ABORT, 'journal position is monotonic');
END;

CREATE TRIGGER IF NOT EXISTS harness_journal_positions_no_delete
BEFORE DELETE ON harness_journal_positions
BEGIN
    SELECT RAISE(ABORT, 'journal position is monotonic');
END;

CREATE TRIGGER IF NOT EXISTS harness_projection_transition
BEFORE UPDATE ON harness_projection_checkpoints
BEGIN
    SELECT CASE
        WHEN NEW.workspace_id <> OLD.workspace_id
            OR NEW.projection_name <> OLD.projection_name
        THEN RAISE(ABORT, 'projection identity is immutable')
        WHEN NEW.updated_at < OLD.updated_at
        THEN RAISE(ABORT, 'projection timestamp is monotonic')
        WHEN NEW.generation = OLD.generation AND (
            NEW.projection_version <> OLD.projection_version
            OR NEW.last_journal_sequence < OLD.last_journal_sequence
            OR NEW.event_count < OLD.event_count
            OR (
                (NEW.last_journal_sequence = OLD.last_journal_sequence)
                <> (NEW.event_count = OLD.event_count)
            )
            OR (
                NEW.last_journal_sequence = OLD.last_journal_sequence
                AND (
                    NEW.state_json <> OLD.state_json
                    OR NEW.state_sha256 <> OLD.state_sha256
                )
            )
            OR (
                OLD.projection_status <> 'healthy'
                AND NEW.projection_status = 'healthy'
            )
        )
        THEN RAISE(ABORT, 'projection checkpoint is monotonic')
        WHEN NEW.generation = OLD.generation + 1 AND (
            NEW.projection_status <> 'healthy'
            OR NEW.failure_code IS NOT NULL
        )
        THEN RAISE(ABORT, 'projection rebuild must finish healthy')
        WHEN NEW.generation NOT IN (OLD.generation, OLD.generation + 1)
        THEN RAISE(ABORT, 'projection generation must advance by one')
    END;
END;

CREATE TRIGGER IF NOT EXISTS harness_journal_health_transition
BEFORE UPDATE ON harness_journal_health
BEGIN
    SELECT CASE
        WHEN NEW.singleton <> OLD.singleton
        THEN RAISE(ABORT, 'journal health identity is immutable')
        WHEN NEW.generation = OLD.generation AND (
            NEW.verified_event_count < OLD.verified_event_count
            OR NEW.verified_at < OLD.verified_at
            OR (
                OLD.journal_status = 'needs_operator'
                AND NEW.journal_status = 'healthy'
            )
        )
        THEN RAISE(ABORT, 'journal health is monotonic')
        WHEN NEW.generation = OLD.generation + 1 AND (
            NEW.journal_status <> 'healthy'
            OR NEW.failure_code IS NOT NULL
        )
        THEN RAISE(ABORT, 'journal recovery must finish healthy')
        WHEN NEW.generation NOT IN (OLD.generation, OLD.generation + 1)
        THEN RAISE(ABORT, 'journal health generation must advance by one')
    END;
END;

CREATE TRIGGER IF NOT EXISTS harness_events_require_healthy_health
BEFORE INSERT ON harness_events
WHEN COALESCE((
    SELECT journal_status FROM harness_journal_health WHERE singleton = 1
), 'needs_operator') <> 'healthy'
BEGIN
    SELECT RAISE(ABORT, 'harness journal requires operator');
END;
""" + (
    SQLITE_RETENTION_SCHEMA
    + SQLITE_RETENTION_EVIDENCE_SCHEMA
    + SQLITE_STORAGE_SCHEMA
    + SQLITE_RECOVERY_SCHEMA
    + SQLITE_SESSION_SCHEMA
    + SQLITE_PROVIDER_COST_SCHEMA
    + SQLITE_APPROVAL_SCHEMA
    + SQLITE_OPERATION_ADMISSION_SCHEMA
)
