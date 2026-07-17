"""Prevent duplicate active deletion and publication work.

Revision ID: 20260717_05
Revises: 20260717_04
"""

from collections.abc import Sequence

from alembic import op

from migrations.active_work_reconciliation_sql import (
    RECONCILE_EXISTING_ACTIVE_WORK,
)
from migrations.active_work_schema_sql import DURABLE_SEARCH_SCHEMA
from migrations.active_work_trigger_sql import (
    LEGACY_ARTIFACT_PROVENANCE_FUNCTION,
    LEGACY_READY_FUNCTION,
    STRICT_READY_FUNCTION,
    TARGETED_ARTIFACT_PROVENANCE_FUNCTION,
)

revision: str = "20260717_05"
down_revision: str | Sequence[str] | None = "20260717_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(DURABLE_SEARCH_SCHEMA)
    op.execute(RECONCILE_EXISTING_ACTIVE_WORK)
    op.execute(
        """
        CREATE UNIQUE INDEX uq_ingestion_jobs_active_delete
            ON ingestion_jobs (
                tenant_id, collection_id, document_version_id
            )
            WHERE stage = 'delete'
              AND state IN ('pending', 'leased', 'running', 'retry_scheduled');

        CREATE UNIQUE INDEX uq_index_publications_one_active_kind
            ON index_publications (
                tenant_id, collection_id, document_version_id,
                chunk_id, publication_kind
            )
            WHERE deleted_at IS NULL;

        CREATE INDEX ix_ingestion_jobs_queue_metrics
            ON ingestion_jobs (
                state, stage, (COALESCE(retry_at, updated_at))
            ) WHERE state IN ('pending', 'retry_scheduled', 'dead_lettered');
        """
    )
    op.execute(TARGETED_ARTIFACT_PROVENANCE_FUNCTION)
    op.execute(STRICT_READY_FUNCTION)


def downgrade() -> None:
    op.execute(LEGACY_READY_FUNCTION)
    op.execute(LEGACY_ARTIFACT_PROVENANCE_FUNCTION)
    op.execute(
        """
        ALTER TABLE version_transition_events
            DROP CONSTRAINT ck_version_transition_operation;
        ALTER TABLE version_transition_events
            ADD CONSTRAINT ck_version_transition_operation CHECK (
                operation IN ('advance', 'retry', 'reprocess', 'cancel',
                    'supersede', 'delete')
            );

        DROP INDEX ix_ingestion_jobs_queue_metrics;
        DROP INDEX uq_index_publications_one_active_kind;
        DROP INDEX uq_ingestion_jobs_active_delete;

        DROP TABLE service_heartbeats;
        DROP TABLE search_cleanup_entries;

        ALTER TABLE index_publications
            DROP CONSTRAINT ck_index_publications_activation,
            DROP CONSTRAINT ck_index_publications_attempt,
            DROP CONSTRAINT fk_index_publications_job,
            DROP COLUMN activated_at,
            DROP COLUMN publication_attempt,
            DROP COLUMN publication_job_id;
        ALTER TABLE artifacts
            DROP CONSTRAINT ck_artifacts_search_target,
            DROP COLUMN search_target_version,
            DROP COLUMN search_target_name;
        ALTER TABLE ingestion_jobs
            DROP CONSTRAINT ck_ingestion_jobs_publication_target,
            DROP COLUMN publication_target_attempt,
            DROP COLUMN publication_target_version,
            DROP COLUMN publication_target_name;
        """
    )
