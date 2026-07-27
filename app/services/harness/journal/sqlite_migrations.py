"""Forward-only local SQLite journal schema migrations."""

from app.services.harness.journal.sqlite_approval_schema import (
    SQLITE_APPROVAL_SCHEMA,
)
from app.services.harness.journal.sqlite_background_job_schema import (
    SQLITE_BACKGROUND_JOB_SCHEMA,
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

SQLITE_MIGRATE_V2_TO_V3 = (
    """
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
"""
    + SQLITE_RETENTION_SCHEMA
    + """
UPDATE harness_journal_schema SET schema_version = 3 WHERE singleton = 1;
COMMIT;
PRAGMA foreign_keys = ON;
"""
)

SQLITE_MIGRATE_V3_TO_V4 = (
    """
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
"""
    + SQLITE_RETENTION_EVIDENCE_SCHEMA
    + """
UPDATE harness_journal_schema SET schema_version = 4 WHERE singleton = 1;
COMMIT;
PRAGMA foreign_keys = ON;
"""
)

SQLITE_MIGRATE_V4_TO_V5 = (
    """
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
"""
    + SQLITE_STORAGE_SCHEMA
    + """
UPDATE harness_journal_schema SET schema_version = 5 WHERE singleton = 1;
COMMIT;
PRAGMA foreign_keys = ON;
"""
)

SQLITE_MIGRATE_V5_TO_V6 = (
    """
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
"""
    + SQLITE_RECOVERY_SCHEMA
    + """
UPDATE harness_journal_schema SET schema_version = 6 WHERE singleton = 1;
COMMIT;
PRAGMA foreign_keys = ON;
"""
)

SQLITE_MIGRATE_V6_TO_V7 = (
    """
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
"""
    + SQLITE_SESSION_SCHEMA
    + """
UPDATE harness_journal_schema SET schema_version = 7 WHERE singleton = 1;
COMMIT;
PRAGMA foreign_keys = ON;
"""
)

SQLITE_MIGRATE_V7_TO_V8 = (
    """
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
"""
    + SQLITE_PROVIDER_COST_SCHEMA
    + """
UPDATE harness_journal_schema SET schema_version = 8 WHERE singleton = 1;
COMMIT;
PRAGMA foreign_keys = ON;
"""
)

SQLITE_MIGRATE_V8_TO_V9 = (
    """
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
"""
    + SQLITE_APPROVAL_SCHEMA
    + """
UPDATE harness_journal_schema SET schema_version = 9 WHERE singleton = 1;
COMMIT;
PRAGMA foreign_keys = ON;
"""
)

SQLITE_MIGRATE_V9_TO_V10 = (
    """
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
"""
    + SQLITE_OPERATION_ADMISSION_SCHEMA
    + """
UPDATE harness_journal_schema SET schema_version = 10 WHERE singleton = 1;
COMMIT;
PRAGMA foreign_keys = ON;
"""
)

SQLITE_MIGRATE_V10_TO_V11 = (
    """
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
"""
    + SQLITE_BACKGROUND_JOB_SCHEMA
    + """
UPDATE harness_journal_schema SET schema_version = 11 WHERE singleton = 1;
COMMIT;
PRAGMA foreign_keys = ON;
"""
)
