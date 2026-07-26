"""Add rebuildable deterministic projection checkpoints.

Revision ID: 20260726_07
Revises: 20260726_06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260726_07"
down_revision: str | Sequence[str] | None = "20260726_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE harness_projection_checkpoints (
            workspace_id CHAR(36) NOT NULL,
            projection_name VARCHAR(128) NOT NULL,
            projection_version VARCHAR(5) NOT NULL,
            generation BIGINT NOT NULL DEFAULT 1,
            last_journal_sequence BIGINT NOT NULL DEFAULT 0,
            event_count BIGINT NOT NULL DEFAULT 0,
            state_json TEXT NOT NULL,
            state_sha256 CHAR(64) NOT NULL,
            projection_status VARCHAR(16) NOT NULL DEFAULT 'healthy',
            failure_code VARCHAR(128),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (workspace_id, projection_name),
            CONSTRAINT ck_harness_projection_workspace CHECK (
                workspace_id ~ '^wsp_[0-9a-f]{32}$'
            ),
            CONSTRAINT ck_harness_projection_name CHECK (
                projection_name ~
                '^[a-z][a-z0-9]*([._-][a-z0-9]+)*$'
            ),
            CONSTRAINT ck_harness_projection_version CHECK (
                projection_version ~ '^1[.][0-9]{1,3}$'
            ),
            CONSTRAINT ck_harness_projection_generation CHECK (
                generation > 0
            ),
            CONSTRAINT ck_harness_projection_sequences CHECK (
                last_journal_sequence >= 0 AND event_count >= 0
            ),
            CONSTRAINT ck_harness_projection_state CHECK (
                octet_length(state_json) <= 4194304
                AND jsonb_typeof(state_json::jsonb) = 'object'
            ),
            CONSTRAINT ck_harness_projection_sha CHECK (
                state_sha256 ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_harness_projection_health CHECK (
                (
                    projection_status = 'healthy'
                    AND failure_code IS NULL
                ) OR (
                    projection_status IN ('diverged', 'needs_operator')
                    AND failure_code ~ '^[a-z][a-z0-9_]{2,127}$'
                )
            )
        );
        CREATE INDEX ix_harness_projection_unhealthy
            ON harness_projection_checkpoints (
                projection_status, updated_at, workspace_id, projection_name
            )
            WHERE projection_status <> 'healthy';

        CREATE FUNCTION enforce_harness_projection_transition()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
                OR NEW.projection_name IS DISTINCT FROM OLD.projection_name THEN
                RAISE EXCEPTION 'projection identity is immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.generation = OLD.generation THEN
                IF NEW.projection_version IS DISTINCT FROM OLD.projection_version
                    OR NEW.last_journal_sequence < OLD.last_journal_sequence
                    OR NEW.event_count < OLD.event_count
                    OR NEW.updated_at < OLD.updated_at
                    OR (
                        (NEW.last_journal_sequence = OLD.last_journal_sequence)
                        IS DISTINCT FROM
                        (NEW.event_count = OLD.event_count)
                    )
                    OR (
                        NEW.last_journal_sequence = OLD.last_journal_sequence
                        AND (
                            NEW.state_json IS DISTINCT FROM OLD.state_json
                            OR NEW.state_sha256 IS DISTINCT FROM OLD.state_sha256
                        )
                    )
                    OR (
                        OLD.projection_status <> 'healthy'
                        AND NEW.projection_status = 'healthy'
                    ) THEN
                    RAISE EXCEPTION 'projection checkpoint is monotonic'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSIF NEW.generation = OLD.generation + 1 THEN
                IF NEW.projection_status <> 'healthy'
                    OR NEW.failure_code IS NOT NULL THEN
                    RAISE EXCEPTION 'projection rebuild must finish healthy'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSE
                RAISE EXCEPTION 'projection generation must advance by one'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER harness_projection_transition
            BEFORE UPDATE ON harness_projection_checkpoints
            FOR EACH ROW EXECUTE FUNCTION enforce_harness_projection_transition();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE harness_projection_checkpoints;
        DROP FUNCTION enforce_harness_projection_transition();
        """
    )
