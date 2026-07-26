"""SQLite schema for transactional provider cost reservations."""

SQLITE_PROVIDER_COST_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_provider_cost_workspaces (
    workspace_id TEXT PRIMARY KEY,
    reserved_microusd INTEGER NOT NULL DEFAULT 0
        CHECK (reserved_microusd BETWEEN 0 AND 10000000000),
    settled_microusd INTEGER NOT NULL DEFAULT 0
        CHECK (settled_microusd BETWEEN 0 AND 10000000000),
    active_reservations INTEGER NOT NULL DEFAULT 0
        CHECK (active_reservations BETWEEN 0 AND 1000000),
    updated_at TEXT NOT NULL,
    CHECK (reserved_microusd + settled_microusd <= 10000000000),
    CHECK (
        length(workspace_id) = 36
        AND substr(workspace_id, 1, 4) = 'wsp_'
        AND substr(workspace_id, 5) NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE TABLE IF NOT EXISTS harness_provider_cost_turns (
    workspace_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    reserved_microusd INTEGER NOT NULL DEFAULT 0
        CHECK (reserved_microusd BETWEEN 0 AND 10000000000),
    settled_microusd INTEGER NOT NULL DEFAULT 0
        CHECK (settled_microusd BETWEEN 0 AND 10000000000),
    active_reservations INTEGER NOT NULL DEFAULT 0
        CHECK (active_reservations BETWEEN 0 AND 1000000),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, turn_id),
    FOREIGN KEY (workspace_id)
        REFERENCES harness_provider_cost_workspaces(workspace_id),
    CHECK (reserved_microusd + settled_microusd <= 10000000000),
    CHECK (
        length(turn_id) = 36
        AND substr(turn_id, 1, 4) = 'trn_'
        AND substr(turn_id, 5) NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE TABLE IF NOT EXISTS harness_provider_cost_reservations (
    reservation_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    provider_request_sha256 TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 64),
    estimated_cost_microusd INTEGER NOT NULL
        CHECK (estimated_cost_microusd BETWEEN 0 AND 10000000000),
    request_json TEXT NOT NULL CHECK (
        length(CAST(request_json AS BLOB)) <= 16384
        AND json_valid(request_json)
        AND json_type(request_json) = 'object'
    ),
    reservation_status TEXT NOT NULL
        CHECK (reservation_status IN ('reserved', 'released', 'settled')),
    actual_cost_microusd INTEGER
        CHECK (actual_cost_microusd BETWEEN 0 AND estimated_cost_microusd),
    requested_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    FOREIGN KEY (workspace_id, turn_id)
        REFERENCES harness_provider_cost_turns(workspace_id, turn_id),
    UNIQUE (workspace_id, provider_request_sha256, attempt),
    CHECK (
        length(reservation_id) = 36
        AND substr(reservation_id, 1, 4) = 'pcs_'
        AND substr(reservation_id, 5) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(provider_request_sha256) = 64
        AND provider_request_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (updated_at >= requested_at),
    CHECK (
        (reservation_status = 'reserved'
            AND actual_cost_microusd IS NULL AND revision = 0)
        OR (reservation_status = 'released'
            AND actual_cost_microusd IS NULL AND revision >= 1)
        OR (reservation_status = 'settled'
            AND actual_cost_microusd IS NOT NULL AND revision >= 1)
    )
);

CREATE INDEX IF NOT EXISTS ix_harness_provider_cost_workspace_status
ON harness_provider_cost_reservations (
    workspace_id, reservation_status, updated_at
);

CREATE INDEX IF NOT EXISTS ix_harness_provider_cost_turn_status
ON harness_provider_cost_reservations (
    workspace_id, turn_id, reservation_status, updated_at
);

CREATE TRIGGER IF NOT EXISTS harness_provider_cost_reservation_transition
BEFORE UPDATE ON harness_provider_cost_reservations
WHEN NEW.reservation_id <> OLD.reservation_id
    OR NEW.workspace_id <> OLD.workspace_id
    OR NEW.turn_id <> OLD.turn_id
    OR NEW.provider_request_sha256 <> OLD.provider_request_sha256
    OR NEW.attempt <> OLD.attempt
    OR NEW.estimated_cost_microusd <> OLD.estimated_cost_microusd
    OR NEW.request_json <> OLD.request_json
    OR NEW.requested_at <> OLD.requested_at
    OR NEW.updated_at < OLD.updated_at
    OR OLD.reservation_status <> 'reserved'
    OR NEW.reservation_status NOT IN ('released', 'settled')
    OR NEW.revision <> OLD.revision + 1
BEGIN
    SELECT RAISE(ABORT, 'provider cost reservation transition is invalid');
END;

CREATE TRIGGER IF NOT EXISTS harness_provider_cost_reservation_no_delete
BEFORE DELETE ON harness_provider_cost_reservations
BEGIN
    SELECT RAISE(ABORT, 'provider cost reservation evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_provider_cost_workspace_transition
BEFORE UPDATE ON harness_provider_cost_workspaces
WHEN NEW.workspace_id <> OLD.workspace_id OR NEW.updated_at < OLD.updated_at
BEGIN
    SELECT RAISE(ABORT, 'provider workspace cost transition is invalid');
END;

CREATE TRIGGER IF NOT EXISTS harness_provider_cost_workspace_no_delete
BEFORE DELETE ON harness_provider_cost_workspaces
BEGIN
    SELECT RAISE(ABORT, 'provider workspace cost evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_provider_cost_turn_transition
BEFORE UPDATE ON harness_provider_cost_turns
WHEN NEW.workspace_id <> OLD.workspace_id
    OR NEW.turn_id <> OLD.turn_id
    OR NEW.updated_at < OLD.updated_at
BEGIN
    SELECT RAISE(ABORT, 'provider turn cost transition is invalid');
END;

CREATE TRIGGER IF NOT EXISTS harness_provider_cost_turn_no_delete
BEFORE DELETE ON harness_provider_cost_turns
BEGIN
    SELECT RAISE(ABORT, 'provider turn cost evidence is immutable');
END;
"""
