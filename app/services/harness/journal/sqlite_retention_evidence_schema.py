"""SQLite schema for durable artifact retention evidence."""

SQLITE_RETENTION_EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_retained_blobs (
    workspace_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL
        CHECK (size_bytes >= 1 AND size_bytes <= 4294967296),
    created_at TEXT NOT NULL,
    garbage_collected_at TEXT,
    PRIMARY KEY (workspace_id, content_sha256),
    CHECK (
        length(workspace_id) = 36
        AND substr(workspace_id, 1, 4) = 'wsp_'
        AND substr(workspace_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(content_sha256) = 64
        AND content_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        garbage_collected_at IS NULL
        OR garbage_collected_at > created_at
    )
);

CREATE TABLE IF NOT EXISTS harness_artifact_references (
    workspace_id TEXT NOT NULL,
    reference_sha256 TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY (workspace_id, reference_sha256),
    FOREIGN KEY (workspace_id, content_sha256)
        REFERENCES harness_retained_blobs(workspace_id, content_sha256),
    CHECK (
        length(reference_sha256) = 64
        AND reference_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (released_at IS NULL OR released_at > created_at)
);

CREATE INDEX IF NOT EXISTS ix_harness_active_artifact_references
ON harness_artifact_references (workspace_id, content_sha256)
WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS harness_artifact_tombstones (
    workspace_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    tombstoned_at TEXT NOT NULL,
    delete_after TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (
        length(reason) BETWEEN 1 AND 2048
        AND instr(reason, char(0)) = 0
    ),
    PRIMARY KEY (workspace_id, content_sha256),
    FOREIGN KEY (workspace_id, content_sha256)
        REFERENCES harness_retained_blobs(workspace_id, content_sha256),
    CHECK (delete_after > tombstoned_at)
);

CREATE INDEX IF NOT EXISTS ix_harness_due_artifact_tombstones
ON harness_artifact_tombstones (workspace_id, delete_after);

CREATE TABLE IF NOT EXISTS harness_artifact_legal_holds (
    workspace_id TEXT NOT NULL,
    hold_sha256 TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    placed_at TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY (workspace_id, hold_sha256),
    FOREIGN KEY (workspace_id, content_sha256)
        REFERENCES harness_retained_blobs(workspace_id, content_sha256),
    CHECK (
        length(hold_sha256) = 64
        AND hold_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (released_at IS NULL OR released_at > placed_at)
);

CREATE INDEX IF NOT EXISTS ix_harness_active_artifact_holds
ON harness_artifact_legal_holds (workspace_id, content_sha256)
WHERE released_at IS NULL;

CREATE TRIGGER IF NOT EXISTS harness_retained_blobs_transition
BEFORE UPDATE ON harness_retained_blobs
WHEN NEW.workspace_id <> OLD.workspace_id
    OR NEW.content_sha256 <> OLD.content_sha256
    OR NEW.size_bytes <> OLD.size_bytes
    OR NEW.created_at <> OLD.created_at
    OR OLD.garbage_collected_at IS NOT NULL
    OR NEW.garbage_collected_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'retained blob transition is invalid');
END;

CREATE TRIGGER IF NOT EXISTS harness_retained_blobs_require_collectable
BEFORE UPDATE OF garbage_collected_at ON harness_retained_blobs
WHEN OLD.garbage_collected_at IS NULL
    AND NEW.garbage_collected_at IS NOT NULL
    AND (
        NOT EXISTS (
            SELECT 1 FROM harness_synced_snapshots AS snapshot
            WHERE snapshot.workspace_id = OLD.workspace_id
        )
        OR EXISTS (
            SELECT 1
            FROM json_each((
                SELECT snapshot.reachable_json
                FROM harness_synced_snapshots AS snapshot
                WHERE snapshot.workspace_id = OLD.workspace_id
                ORDER BY snapshot.through_journal_sequence DESC
                LIMIT 1
            )) AS reachable
            WHERE reachable.value = OLD.content_sha256
        )
        OR NOT EXISTS (
            SELECT 1 FROM harness_artifact_tombstones AS tombstone
            WHERE tombstone.workspace_id = OLD.workspace_id
              AND tombstone.content_sha256 = OLD.content_sha256
              AND tombstone.delete_after <= NEW.garbage_collected_at
        )
        OR EXISTS (
            SELECT 1 FROM harness_artifact_references AS reference
            WHERE reference.workspace_id = OLD.workspace_id
              AND reference.content_sha256 = OLD.content_sha256
              AND reference.released_at IS NULL
        )
        OR EXISTS (
            SELECT 1 FROM harness_artifact_legal_holds AS legal_hold
            WHERE legal_hold.workspace_id = OLD.workspace_id
              AND legal_hold.content_sha256 = OLD.content_sha256
              AND legal_hold.released_at IS NULL
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'retained blob is not collectable');
END;

CREATE TRIGGER IF NOT EXISTS harness_retained_blobs_no_delete
BEFORE DELETE ON harness_retained_blobs
BEGIN
    SELECT RAISE(ABORT, 'retained blob evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_artifact_references_transition
BEFORE UPDATE ON harness_artifact_references
WHEN NEW.workspace_id <> OLD.workspace_id
    OR NEW.reference_sha256 <> OLD.reference_sha256
    OR NEW.content_sha256 <> OLD.content_sha256
    OR NEW.created_at <> OLD.created_at
    OR OLD.released_at IS NOT NULL
    OR NEW.released_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'artifact reference transition is invalid');
END;

CREATE TRIGGER IF NOT EXISTS harness_artifact_references_no_delete
BEFORE DELETE ON harness_artifact_references
BEGIN
    SELECT RAISE(ABORT, 'artifact reference evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_artifact_tombstones_no_update
BEFORE UPDATE ON harness_artifact_tombstones
BEGIN
    SELECT RAISE(ABORT, 'artifact tombstones are immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_artifact_tombstones_no_delete
BEFORE DELETE ON harness_artifact_tombstones
BEGIN
    SELECT RAISE(ABORT, 'artifact tombstones are immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_artifact_holds_transition
BEFORE UPDATE ON harness_artifact_legal_holds
WHEN NEW.workspace_id <> OLD.workspace_id
    OR NEW.hold_sha256 <> OLD.hold_sha256
    OR NEW.content_sha256 <> OLD.content_sha256
    OR NEW.placed_at <> OLD.placed_at
    OR OLD.released_at IS NOT NULL
    OR NEW.released_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'artifact legal hold transition is invalid');
END;

CREATE TRIGGER IF NOT EXISTS harness_artifact_holds_no_delete
BEFORE DELETE ON harness_artifact_legal_holds
BEGIN
    SELECT RAISE(ABORT, 'artifact legal hold evidence is immutable');
END;
"""
