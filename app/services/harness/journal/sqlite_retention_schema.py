"""SQLite schema for immutable replay snapshots and sealed segments."""

SQLITE_RETENTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_synced_snapshots (
    workspace_id TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    through_journal_sequence INTEGER NOT NULL
        CHECK (
            through_journal_sequence >= 0
            AND through_journal_sequence <= 9223372036854775807
        ),
    reachable_json TEXT NOT NULL CHECK (
        length(CAST(reachable_json AS BLOB)) <= 1048576
        AND json_valid(reachable_json)
        AND json_type(reachable_json) = 'array'
    ),
    synced_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, snapshot_sha256),
    UNIQUE (workspace_id, through_journal_sequence),
    CHECK (
        length(workspace_id) = 36
        AND substr(workspace_id, 1, 4) = 'wsp_'
        AND substr(workspace_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(snapshot_sha256) = 64
        AND snapshot_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(manifest_sha256) = 64
        AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE INDEX IF NOT EXISTS ix_harness_snapshots_latest
ON harness_synced_snapshots (
    workspace_id, through_journal_sequence DESC
);

CREATE TABLE IF NOT EXISTS harness_sealed_segments (
    workspace_id TEXT NOT NULL,
    segment_sha256 TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    first_journal_sequence INTEGER NOT NULL
        CHECK (first_journal_sequence >= 1),
    last_journal_sequence INTEGER NOT NULL
        CHECK (last_journal_sequence >= first_journal_sequence),
    event_count INTEGER NOT NULL CHECK (event_count >= 1),
    sealed_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, segment_sha256),
    UNIQUE (workspace_id, first_journal_sequence, last_journal_sequence),
    FOREIGN KEY (workspace_id, snapshot_sha256)
        REFERENCES harness_synced_snapshots(workspace_id, snapshot_sha256),
    CHECK (
        length(workspace_id) = 36
        AND substr(workspace_id, 1, 4) = 'wsp_'
        AND substr(workspace_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(segment_sha256) = 64
        AND segment_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(snapshot_sha256) = 64
        AND snapshot_sha256 NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE INDEX IF NOT EXISTS ix_harness_segments_snapshot
ON harness_sealed_segments (workspace_id, snapshot_sha256);

CREATE TRIGGER IF NOT EXISTS harness_synced_snapshots_no_update
BEFORE UPDATE ON harness_synced_snapshots
BEGIN
    SELECT RAISE(ABORT, 'synced snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_synced_snapshots_no_delete
BEFORE DELETE ON harness_synced_snapshots
BEGIN
    SELECT RAISE(ABORT, 'synced snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_sealed_segments_no_update
BEFORE UPDATE ON harness_sealed_segments
BEGIN
    SELECT RAISE(ABORT, 'sealed segments are immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_sealed_segments_no_delete
BEFORE DELETE ON harness_sealed_segments
BEGIN
    SELECT RAISE(ABORT, 'sealed segments are immutable');
END;
"""
