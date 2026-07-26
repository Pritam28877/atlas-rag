"""Parameterized PostgreSQL statements for projection checkpoints."""

PROJECTION_COLUMNS = """
generation, workspace_id, projection_name, projection_version,
last_journal_sequence, event_count, state_json, state_sha256,
projection_status, failure_code, updated_at
"""

LOAD_PROJECTION = f"""
SELECT {PROJECTION_COLUMNS}
FROM harness_projection_checkpoints
WHERE workspace_id = :workspace_id
  AND projection_name = :projection_name
"""

INSERT_PROJECTION = f"""
INSERT INTO harness_projection_checkpoints (
    workspace_id, projection_name, projection_version, generation,
    last_journal_sequence, event_count, state_json, state_sha256,
    projection_status, failure_code, updated_at
) VALUES (
    :workspace_id, :projection_name, :projection_version, 1,
    :last_journal_sequence, :event_count, :state_json, :state_sha256,
    'healthy', NULL, clock_timestamp()
)
ON CONFLICT (workspace_id, projection_name) DO NOTHING
RETURNING {PROJECTION_COLUMNS}
"""

ADVANCE_PROJECTION = f"""
UPDATE harness_projection_checkpoints
SET projection_version = :projection_version,
    last_journal_sequence = :last_journal_sequence,
    event_count = :event_count,
    state_json = :state_json,
    state_sha256 = :state_sha256,
    updated_at = clock_timestamp()
WHERE workspace_id = :workspace_id
  AND projection_name = :projection_name
  AND generation = :expected_generation
  AND last_journal_sequence = :expected_sequence
  AND projection_status = 'healthy'
RETURNING {PROJECTION_COLUMNS}
"""

REBUILD_PROJECTION = f"""
UPDATE harness_projection_checkpoints
SET projection_version = :projection_version,
    generation = generation + 1,
    last_journal_sequence = :last_journal_sequence,
    event_count = :event_count,
    state_json = :state_json,
    state_sha256 = :state_sha256,
    projection_status = 'healthy',
    failure_code = NULL,
    updated_at = clock_timestamp()
WHERE workspace_id = :workspace_id
  AND projection_name = :projection_name
  AND generation = :expected_generation
  AND last_journal_sequence = :expected_sequence
RETURNING {PROJECTION_COLUMNS}
"""

MARK_PROJECTION_UNHEALTHY = f"""
UPDATE harness_projection_checkpoints
SET projection_status = :projection_status,
    failure_code = :failure_code,
    updated_at = clock_timestamp()
WHERE workspace_id = :workspace_id
  AND projection_name = :projection_name
  AND generation = :expected_generation
  AND last_journal_sequence = :expected_sequence
RETURNING {PROJECTION_COLUMNS}
"""
