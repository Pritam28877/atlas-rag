"""Require an existing healthy journal row before PostgreSQL event inserts.

Revision ID: 20260727_09
Revises: 20260727_08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260727_09"
down_revision: str | Sequence[str] | None = "20260727_08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION require_healthy_harness_journal()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM harness_journal_health
                WHERE singleton AND journal_status = 'healthy'
            ) THEN
                RAISE EXCEPTION 'harness journal requires operator'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION require_healthy_harness_journal()
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
        """
    )
