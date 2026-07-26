"""Add the workspace-scoped Atlas Harness event journal.

Revision ID: 20260726_06
Revises: 20260717_05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260726_06"
down_revision: str | Sequence[str] | None = "20260717_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE harness_journal_positions (
            workspace_id CHAR(36) PRIMARY KEY,
            current_sequence BIGINT NOT NULL DEFAULT 0,
            CONSTRAINT ck_harness_journal_position_workspace CHECK (
                workspace_id ~ '^wsp_[0-9a-f]{32}$'
            ),
            CONSTRAINT ck_harness_journal_position_sequence CHECK (
                current_sequence >= 0
            )
        );

        CREATE TABLE harness_journal_aggregates (
            workspace_id CHAR(36) NOT NULL,
            aggregate_id CHAR(36) NOT NULL,
            current_sequence BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (workspace_id, aggregate_id),
            CONSTRAINT ck_harness_journal_aggregate_workspace CHECK (
                workspace_id ~ '^wsp_[0-9a-f]{32}$'
            ),
            CONSTRAINT ck_harness_journal_aggregate_id CHECK (
                aggregate_id ~
                '^(wsp|thr|trn|opn|apr|tsk|art|pvd|evl)_[0-9a-f]{32}$'
            ),
            CONSTRAINT ck_harness_journal_aggregate_sequence CHECK (
                current_sequence >= 0
            )
        );

        CREATE TABLE harness_journal_events (
            journal_sequence BIGINT NOT NULL,
            event_id CHAR(36) NOT NULL,
            workspace_id CHAR(36) NOT NULL,
            aggregate_id CHAR(36) NOT NULL,
            aggregate_sequence BIGINT NOT NULL,
            event_json TEXT NOT NULL,
            event_sha256 CHAR(64) NOT NULL,
            request_sha256 CHAR(64) NOT NULL,
            durability VARCHAR(16) NOT NULL,
            committed_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (workspace_id, journal_sequence),
            FOREIGN KEY (workspace_id, aggregate_id)
                REFERENCES harness_journal_aggregates(
                    workspace_id, aggregate_id
                ),
            CONSTRAINT uq_harness_journal_event_id
                UNIQUE (workspace_id, event_id),
            CONSTRAINT uq_harness_journal_aggregate_sequence
                UNIQUE (workspace_id, aggregate_id, aggregate_sequence),
            CONSTRAINT ck_harness_journal_sequence CHECK (
                journal_sequence > 0
            ),
            CONSTRAINT ck_harness_journal_event_id CHECK (
                event_id ~ '^evt_[0-9a-f]{32}$'
            ),
            CONSTRAINT ck_harness_journal_event_sequence CHECK (
                aggregate_sequence > 0
            ),
            CONSTRAINT ck_harness_journal_event_size CHECK (
                octet_length(event_json) <= 4194304
            ),
            CONSTRAINT ck_harness_journal_event_sha CHECK (
                event_sha256 ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_harness_journal_request_sha CHECK (
                request_sha256 ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_harness_journal_durability CHECK (
                durability IN ('synchronous', 'buffered')
            )
        );

        CREATE TABLE harness_journal_idempotency (
            workspace_id CHAR(36) NOT NULL,
            aggregate_id CHAR(36) NOT NULL,
            idempotency_key VARCHAR(128) NOT NULL,
            request_sha256 CHAR(64) NOT NULL,
            result_json TEXT NOT NULL,
            PRIMARY KEY (workspace_id, aggregate_id, idempotency_key),
            FOREIGN KEY (workspace_id, aggregate_id)
                REFERENCES harness_journal_aggregates(
                    workspace_id, aggregate_id
                ),
            CONSTRAINT ck_harness_journal_idempotency_key CHECK (
                idempotency_key ~
                '^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$'
            ),
            CONSTRAINT ck_harness_journal_idempotency_sha CHECK (
                request_sha256 ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_harness_journal_result_size CHECK (
                octet_length(result_json) <= 4194304
            )
        );

        CREATE FUNCTION reject_harness_journal_fact_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'harness journal facts are immutable'
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$;
        CREATE TRIGGER harness_journal_events_immutable
            BEFORE UPDATE OR DELETE ON harness_journal_events
            FOR EACH ROW EXECUTE FUNCTION reject_harness_journal_fact_mutation();
        CREATE TRIGGER harness_journal_idempotency_immutable
            BEFORE UPDATE OR DELETE ON harness_journal_idempotency
            FOR EACH ROW EXECUTE FUNCTION reject_harness_journal_fact_mutation();

        CREATE FUNCTION protect_harness_journal_aggregate()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'harness journal aggregate is monotonic'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
                OR NEW.aggregate_id IS DISTINCT FROM OLD.aggregate_id
                OR NEW.current_sequence < OLD.current_sequence THEN
                RAISE EXCEPTION 'harness journal aggregate is monotonic'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER harness_journal_aggregates_monotonic
            BEFORE UPDATE OR DELETE ON harness_journal_aggregates
            FOR EACH ROW EXECUTE FUNCTION protect_harness_journal_aggregate();

        CREATE FUNCTION protect_harness_journal_position()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'harness journal position is monotonic'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
                OR NEW.current_sequence < OLD.current_sequence THEN
                RAISE EXCEPTION 'harness journal position is monotonic'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER harness_journal_positions_monotonic
            BEFORE UPDATE OR DELETE ON harness_journal_positions
            FOR EACH ROW EXECUTE FUNCTION protect_harness_journal_position();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE harness_journal_idempotency;
        DROP TABLE harness_journal_events;
        DROP TABLE harness_journal_aggregates;
        DROP TABLE harness_journal_positions;
        DROP FUNCTION reject_harness_journal_fact_mutation();
        DROP FUNCTION protect_harness_journal_aggregate();
        DROP FUNCTION protect_harness_journal_position();
        """
    )
