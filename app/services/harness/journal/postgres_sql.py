"""Parameterized PostgreSQL statements for the Atlas event journal."""

INSERT_AGGREGATE = """
INSERT INTO harness_journal_aggregates (
    workspace_id, aggregate_id, current_sequence
) VALUES (:workspace_id, :aggregate_id, 0)
ON CONFLICT (workspace_id, aggregate_id) DO NOTHING
"""

LOCK_AGGREGATE = """
SELECT current_sequence
FROM harness_journal_aggregates
WHERE workspace_id = :workspace_id AND aggregate_id = :aggregate_id
FOR UPDATE
"""

INSERT_POSITION = """
INSERT INTO harness_journal_positions (workspace_id, current_sequence)
VALUES (:workspace_id, 0)
ON CONFLICT (workspace_id) DO NOTHING
"""

LOCK_POSITION = """
SELECT current_sequence
FROM harness_journal_positions
WHERE workspace_id = :workspace_id
FOR UPDATE
"""

READ_IDEMPOTENCY = """
SELECT request_sha256, result_json
FROM harness_journal_idempotency
WHERE workspace_id = :workspace_id
  AND aggregate_id = :aggregate_id
  AND idempotency_key = :idempotency_key
"""

INSERT_EVENTS_PREFIX = """
INSERT INTO harness_journal_events (
    journal_sequence, event_id, workspace_id, aggregate_id, aggregate_sequence,
    event_json, event_sha256, request_sha256, durability, committed_at
) VALUES
"""

UPDATE_AGGREGATE = """
UPDATE harness_journal_aggregates
SET current_sequence = :last_sequence
WHERE workspace_id = :workspace_id
  AND aggregate_id = :aggregate_id
  AND current_sequence = :expected_sequence
RETURNING current_sequence
"""

UPDATE_POSITION = """
UPDATE harness_journal_positions
SET current_sequence = :last_journal_sequence
WHERE workspace_id = :workspace_id
  AND current_sequence = :expected_journal_sequence
RETURNING current_sequence
"""

INSERT_IDEMPOTENCY = """
INSERT INTO harness_journal_idempotency (
    workspace_id, aggregate_id, idempotency_key, request_sha256, result_json
) VALUES (
    :workspace_id, :aggregate_id, :idempotency_key, :request_sha256, :result_json
)
"""

READ_AGGREGATE = """
SELECT event_json
FROM harness_journal_events
WHERE workspace_id = :workspace_id
  AND aggregate_id = :aggregate_id
  AND aggregate_sequence > :after_sequence
ORDER BY aggregate_sequence
LIMIT :query_limit
"""

READ_GLOBAL = """
SELECT journal_sequence, event_json
FROM harness_journal_events
WHERE workspace_id = :workspace_id
  AND journal_sequence > :after_journal_sequence
ORDER BY journal_sequence
LIMIT :query_limit
"""

SYNCHRONOUS_COMMIT = "SET LOCAL synchronous_commit TO ON"
BUFFERED_COMMIT = "SET LOCAL synchronous_commit TO OFF"
DATABASE_TIMESTAMP = "SELECT clock_timestamp()"
