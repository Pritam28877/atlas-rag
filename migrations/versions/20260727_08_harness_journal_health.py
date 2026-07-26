"""Add fail-closed journal operator health state.

Revision ID: 20260727_08
Revises: 20260726_07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260727_08"
down_revision: str | Sequence[str] | None = "20260726_07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE harness_journal_health (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
            generation BIGINT NOT NULL DEFAULT 1 CHECK (generation > 0),
            journal_status VARCHAR(16) NOT NULL DEFAULT 'healthy',
            failure_code VARCHAR(128),
            verified_event_count BIGINT NOT NULL DEFAULT 0
                CHECK (verified_event_count >= 0),
            verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_harness_journal_health CHECK (
                (
                    journal_status = 'healthy'
                    AND failure_code IS NULL
                ) OR (
                    journal_status = 'needs_operator'
                    AND failure_code ~ '^[a-z][a-z0-9_]{2,127}$'
                )
            )
        );
        INSERT INTO harness_journal_health (singleton) VALUES (TRUE);

        CREATE FUNCTION enforce_harness_journal_health_transition()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.singleton IS DISTINCT FROM OLD.singleton THEN
                RAISE EXCEPTION 'journal health identity is immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.generation = OLD.generation THEN
                IF NEW.verified_event_count < OLD.verified_event_count
                    OR NEW.verified_at < OLD.verified_at
                    OR (
                        OLD.journal_status = 'needs_operator'
                        AND NEW.journal_status = 'healthy'
                    ) THEN
                    RAISE EXCEPTION 'journal health is monotonic'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSIF NEW.generation = OLD.generation + 1 THEN
                IF NEW.journal_status <> 'healthy'
                    OR NEW.failure_code IS NOT NULL THEN
                    RAISE EXCEPTION 'journal recovery must finish healthy'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSE
                RAISE EXCEPTION 'journal health generation must advance by one'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER harness_journal_health_transition
            BEFORE UPDATE ON harness_journal_health
            FOR EACH ROW
            EXECUTE FUNCTION enforce_harness_journal_health_transition();

        CREATE FUNCTION require_healthy_harness_journal()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM harness_journal_health
                WHERE singleton AND journal_status = 'needs_operator'
            ) THEN
                RAISE EXCEPTION 'harness journal requires operator'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER harness_journal_events_require_health
            BEFORE INSERT ON harness_journal_events
            FOR EACH ROW EXECUTE FUNCTION require_healthy_harness_journal();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER harness_journal_events_require_health
            ON harness_journal_events;
        DROP FUNCTION require_healthy_harness_journal();
        DROP TABLE harness_journal_health;
        DROP FUNCTION enforce_harness_journal_health_transition();
        """
    )
