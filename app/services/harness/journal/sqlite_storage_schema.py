"""SQLite schema for durable workspace blob admission and reservations."""

SQLITE_STORAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_workspace_storage (
    workspace_id TEXT PRIMARY KEY,
    used_blob_bytes INTEGER NOT NULL DEFAULT 0
        CHECK (used_blob_bytes >= 0 AND used_blob_bytes <= 68719476736),
    reserved_blob_bytes INTEGER NOT NULL DEFAULT 0
        CHECK (reserved_blob_bytes >= 0 AND reserved_blob_bytes <= 68719476736),
    sealed INTEGER NOT NULL DEFAULT 0 CHECK (sealed IN (0, 1)),
    seal_reason TEXT CHECK (
        seal_reason IS NULL
        OR seal_reason IN (
            'already_sealed',
            'disk_reserve',
            'workspace_quota'
        )
    ),
    updated_at TEXT NOT NULL,
    CHECK (
        (sealed = 0 AND seal_reason IS NULL)
        OR (sealed = 1 AND seal_reason IS NOT NULL)
    ),
    CHECK (
        length(workspace_id) = 36
        AND substr(workspace_id, 1, 4) = 'wsp_'
        AND substr(workspace_id, 5) NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE TABLE IF NOT EXISTS harness_blob_reservations (
    workspace_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL
        CHECK (size_bytes >= 1 AND size_bytes <= 4294967296),
    reservation_status TEXT NOT NULL
        CHECK (reservation_status IN ('reserved', 'committed', 'released')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, content_sha256),
    FOREIGN KEY (workspace_id)
        REFERENCES harness_workspace_storage(workspace_id),
    CHECK (
        length(content_sha256) = 64
        AND content_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (updated_at >= created_at)
);

CREATE INDEX IF NOT EXISTS ix_harness_active_blob_reservations
ON harness_blob_reservations (workspace_id, updated_at)
WHERE reservation_status = 'reserved';

CREATE TRIGGER IF NOT EXISTS harness_workspace_storage_transition
BEFORE UPDATE ON harness_workspace_storage
WHEN NEW.workspace_id <> OLD.workspace_id
    OR NEW.updated_at < OLD.updated_at
    OR (
        OLD.sealed = 1
        AND (NEW.sealed <> 1 OR NEW.seal_reason <> OLD.seal_reason)
    )
BEGIN
    SELECT RAISE(ABORT, 'workspace storage transition is invalid');
END;

CREATE TRIGGER IF NOT EXISTS harness_workspace_storage_no_delete
BEFORE DELETE ON harness_workspace_storage
BEGIN
    SELECT RAISE(ABORT, 'workspace storage evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_blob_reservations_transition
BEFORE UPDATE ON harness_blob_reservations
WHEN NEW.workspace_id <> OLD.workspace_id
    OR NEW.content_sha256 <> OLD.content_sha256
    OR NEW.size_bytes <> OLD.size_bytes
    OR NEW.created_at <> OLD.created_at
    OR NEW.updated_at < OLD.updated_at
    OR OLD.reservation_status = 'committed'
    OR (
        OLD.reservation_status = 'reserved'
        AND NEW.reservation_status NOT IN ('committed', 'released')
    )
    OR (
        OLD.reservation_status = 'released'
        AND NEW.reservation_status <> 'reserved'
    )
BEGIN
    SELECT RAISE(ABORT, 'blob reservation transition is invalid');
END;

CREATE TRIGGER IF NOT EXISTS harness_blob_reservations_no_delete
BEFORE DELETE ON harness_blob_reservations
BEGIN
    SELECT RAISE(ABORT, 'blob reservation evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_blob_reservation_insert_usage
AFTER INSERT ON harness_blob_reservations
WHEN NEW.reservation_status = 'reserved'
BEGIN
    UPDATE harness_workspace_storage
    SET reserved_blob_bytes = reserved_blob_bytes + NEW.size_bytes,
        updated_at = MAX(updated_at, NEW.updated_at)
    WHERE workspace_id = NEW.workspace_id;
END;

CREATE TRIGGER IF NOT EXISTS harness_blob_reservation_commit_usage
AFTER UPDATE OF reservation_status ON harness_blob_reservations
WHEN OLD.reservation_status = 'reserved'
    AND NEW.reservation_status = 'committed'
BEGIN
    UPDATE harness_workspace_storage
    SET reserved_blob_bytes = reserved_blob_bytes - NEW.size_bytes,
        used_blob_bytes = used_blob_bytes + NEW.size_bytes,
        updated_at = MAX(updated_at, NEW.updated_at)
    WHERE workspace_id = NEW.workspace_id;
END;

CREATE TRIGGER IF NOT EXISTS harness_blob_reservation_release_usage
AFTER UPDATE OF reservation_status ON harness_blob_reservations
WHEN OLD.reservation_status = 'reserved'
    AND NEW.reservation_status = 'released'
BEGIN
    UPDATE harness_workspace_storage
    SET reserved_blob_bytes = reserved_blob_bytes - NEW.size_bytes,
        updated_at = MAX(updated_at, NEW.updated_at)
    WHERE workspace_id = NEW.workspace_id;
END;

CREATE TRIGGER IF NOT EXISTS harness_blob_reservation_retry_usage
AFTER UPDATE OF reservation_status ON harness_blob_reservations
WHEN OLD.reservation_status = 'released'
    AND NEW.reservation_status = 'reserved'
BEGIN
    UPDATE harness_workspace_storage
    SET reserved_blob_bytes = reserved_blob_bytes + NEW.size_bytes,
        updated_at = MAX(updated_at, NEW.updated_at)
    WHERE workspace_id = NEW.workspace_id;
END;

CREATE TRIGGER IF NOT EXISTS harness_collected_blob_usage
AFTER UPDATE OF garbage_collected_at ON harness_retained_blobs
WHEN OLD.garbage_collected_at IS NULL
    AND NEW.garbage_collected_at IS NOT NULL
BEGIN
    UPDATE harness_workspace_storage
    SET used_blob_bytes = used_blob_bytes - OLD.size_bytes,
        updated_at = MAX(updated_at, NEW.garbage_collected_at)
    WHERE workspace_id = NEW.workspace_id;
END;
"""
