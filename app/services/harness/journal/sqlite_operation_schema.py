"""SQLite schema for immutable synchronous operation admission receipts."""

SQLITE_OPERATION_ADMISSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_operation_admission_receipts (
    workspace_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    operation_state TEXT NOT NULL CHECK (
        operation_state IN ('prepared', 'dispatched')
    ),
    fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
    receipt_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL CHECK (
        length(CAST(receipt_json AS BLOB)) <= 32768
        AND json_valid(receipt_json)
        AND json_type(receipt_json) = 'object'
        AND json_extract(receipt_json, '$.workspace_id') = workspace_id
        AND json_extract(receipt_json, '$.operation_id') = operation_id
        AND json_extract(receipt_json, '$.state') = operation_state
        AND json_extract(receipt_json, '$.fencing_token') = fencing_token
        AND json_extract(receipt_json, '$.receipt_sha256') = receipt_sha256
    ),
    durable_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, operation_id, operation_state),
    FOREIGN KEY (workspace_id, operation_id)
        REFERENCES harness_recovery_operations(workspace_id, operation_id),
    CHECK (
        length(receipt_sha256) = 64
        AND receipt_sha256 NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE TRIGGER IF NOT EXISTS harness_operation_admission_no_update
BEFORE UPDATE ON harness_operation_admission_receipts
BEGIN
    SELECT RAISE(ABORT, 'operation admission receipts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS harness_operation_admission_no_delete
BEFORE DELETE ON harness_operation_admission_receipts
BEGIN
    SELECT RAISE(ABORT, 'operation admission receipts are immutable');
END;
"""
