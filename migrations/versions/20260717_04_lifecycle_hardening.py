"""Add immutable reprocessing lineage and lifecycle audit context.

Revision ID: 20260717_04
Revises: 20260716_03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260717_04"
down_revision: str | Sequence[str] | None = "20260716_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE document_versions
            ADD COLUMN reprocessed_from_version_id UUID;
        ALTER TABLE document_versions
            ADD CONSTRAINT fk_document_versions_reprocessed_from
            FOREIGN KEY (
                tenant_id, collection_id, reprocessed_from_version_id
            ) REFERENCES document_versions(tenant_id, collection_id, id)
            ON DELETE RESTRICT;
        ALTER TABLE document_versions
            ADD CONSTRAINT ck_document_versions_reprocessed_not_self
            CHECK (
                reprocessed_from_version_id IS NULL
                OR reprocessed_from_version_id <> id
            );
        CREATE INDEX ix_document_versions_reprocessed_from
            ON document_versions (
                tenant_id, collection_id, reprocessed_from_version_id
            ) WHERE reprocessed_from_version_id IS NOT NULL;

        ALTER TABLE lifecycle_requests
            ADD COLUMN source_document_version_id UUID;
        UPDATE lifecycle_requests
            SET source_document_version_id = document_version_id;
        ALTER TABLE lifecycle_requests
            ALTER COLUMN source_document_version_id SET NOT NULL;
        ALTER TABLE lifecycle_requests
            ADD CONSTRAINT fk_lifecycle_requests_source_version
            FOREIGN KEY (
                tenant_id, collection_id, source_document_version_id
            ) REFERENCES document_versions(tenant_id, collection_id, id)
            ON DELETE RESTRICT;

        ALTER TABLE lifecycle_requests
            ADD CONSTRAINT uq_lifecycle_requests_scope_id
            UNIQUE (tenant_id, collection_id, id);

        ALTER TABLE ingestion_jobs
            ADD COLUMN lifecycle_request_id UUID;
        ALTER TABLE ingestion_jobs
            ADD CONSTRAINT fk_ingestion_jobs_lifecycle_request
            FOREIGN KEY (tenant_id, collection_id, lifecycle_request_id)
            REFERENCES lifecycle_requests(tenant_id, collection_id, id)
            ON DELETE RESTRICT;
        CREATE INDEX ix_ingestion_jobs_lifecycle_request
            ON ingestion_jobs (tenant_id, collection_id, lifecycle_request_id)
            WHERE lifecycle_request_id IS NOT NULL;

        CREATE TABLE object_cleanup_entries (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL,
            collection_id UUID NOT NULL,
            document_version_id UUID NOT NULL,
            lifecycle_request_id UUID NOT NULL,
            object_key VARCHAR(1024) NOT NULL,
            state VARCHAR(16) NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            deleted_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, collection_id, document_version_id)
                REFERENCES document_versions(tenant_id, collection_id, id)
                ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, collection_id, lifecycle_request_id)
                REFERENCES lifecycle_requests(tenant_id, collection_id, id)
                ON DELETE RESTRICT,
            CONSTRAINT ck_object_cleanup_state
                CHECK (state IN ('pending', 'deleted')),
            CONSTRAINT ck_object_cleanup_attempts CHECK (attempt_count >= 0),
            CONSTRAINT ck_object_cleanup_deleted CHECK (
                (state = 'pending' AND deleted_at IS NULL)
                OR (state = 'deleted' AND deleted_at IS NOT NULL)
            ),
            CONSTRAINT uq_object_cleanup_scope_id
                UNIQUE (tenant_id, collection_id, id),
            CONSTRAINT uq_object_cleanup_key UNIQUE (
                tenant_id, collection_id, document_version_id,
                lifecycle_request_id, object_key
            )
        );
        CREATE INDEX ix_object_cleanup_pending
            ON object_cleanup_entries (
                tenant_id, document_version_id, lifecycle_request_id,
                state, object_key
            ) WHERE state = 'pending';

        CREATE OR REPLACE FUNCTION protect_document_version_identity()
        RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.tenant_id, NEW.collection_id, NEW.document_id,
                NEW.version_number, NEW.idempotency_key, NEW.content_sha256,
                NEW.size_bytes, NEW.object_key, NEW.pipeline_profile,
                NEW.reprocessed_from_version_id)
                IS DISTINCT FROM
               (OLD.tenant_id, OLD.collection_id, OLD.document_id,
                OLD.version_number, OLD.idempotency_key, OLD.content_sha256,
                OLD.size_bytes, OLD.object_key, OLD.pipeline_profile,
                OLD.reprocessed_from_version_id) THEN
                RAISE EXCEPTION 'document version identity is immutable';
            END IF;
            IF (NEW.state IS DISTINCT FROM OLD.state
                    AND NEW.state_revision <> OLD.state_revision + 1)
                OR (NEW.state IS NOT DISTINCT FROM OLD.state
                    AND NEW.state_revision <> OLD.state_revision)
                OR NEW.progress_completed < OLD.progress_completed
                OR NEW.progress_total < OLD.progress_total THEN
                RAISE EXCEPTION 'document version progress is monotonic';
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE FUNCTION enforce_reprocessing_lineage() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.reprocessed_from_version_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM document_versions source
                WHERE source.tenant_id = NEW.tenant_id
                  AND source.collection_id = NEW.collection_id
                  AND source.id = NEW.reprocessed_from_version_id
                  AND source.document_id = NEW.document_id
            ) THEN
                RAISE EXCEPTION 'reprocessing lineage must stay within one document';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER document_versions_lineage_same_document
            BEFORE INSERT OR UPDATE OF reprocessed_from_version_id
            ON document_versions
            FOR EACH ROW EXECUTE FUNCTION enforce_reprocessing_lineage();

        CREATE FUNCTION protect_lifecycle_request_identity() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.id, NEW.tenant_id, NEW.collection_id,
                NEW.document_version_id, NEW.source_document_version_id,
                NEW.request_type, NEW.idempotency_key, NEW.requested_by,
                NEW.requested_at)
                IS DISTINCT FROM
               (OLD.id, OLD.tenant_id, OLD.collection_id,
                OLD.document_version_id, OLD.source_document_version_id,
                OLD.request_type, OLD.idempotency_key, OLD.requested_by,
                OLD.requested_at) THEN
                RAISE EXCEPTION 'lifecycle request identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER lifecycle_requests_identity_immutable
            BEFORE UPDATE ON lifecycle_requests
            FOR EACH ROW EXECUTE FUNCTION protect_lifecycle_request_identity();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER lifecycle_requests_identity_immutable
            ON lifecycle_requests;
        DROP FUNCTION protect_lifecycle_request_identity();
        DROP TRIGGER document_versions_lineage_same_document
            ON document_versions;
        DROP FUNCTION enforce_reprocessing_lineage();
        CREATE OR REPLACE FUNCTION protect_document_version_identity()
        RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.tenant_id, NEW.collection_id, NEW.document_id,
                NEW.version_number, NEW.idempotency_key, NEW.content_sha256,
                NEW.size_bytes, NEW.object_key, NEW.pipeline_profile)
                IS DISTINCT FROM
               (OLD.tenant_id, OLD.collection_id, OLD.document_id,
                OLD.version_number, OLD.idempotency_key, OLD.content_sha256,
                OLD.size_bytes, OLD.object_key, OLD.pipeline_profile) THEN
                RAISE EXCEPTION 'document version identity is immutable';
            END IF;
            IF (NEW.state IS DISTINCT FROM OLD.state
                    AND NEW.state_revision <> OLD.state_revision + 1)
                OR (NEW.state IS NOT DISTINCT FROM OLD.state
                    AND NEW.state_revision <> OLD.state_revision)
                OR NEW.progress_completed < OLD.progress_completed
                OR NEW.progress_total < OLD.progress_total THEN
                RAISE EXCEPTION 'document version progress is monotonic';
            END IF;
            RETURN NEW;
        END;
        $$;
        DROP TABLE object_cleanup_entries;
        DROP INDEX ix_ingestion_jobs_lifecycle_request;
        ALTER TABLE ingestion_jobs
            DROP CONSTRAINT fk_ingestion_jobs_lifecycle_request;
        ALTER TABLE ingestion_jobs
            DROP COLUMN lifecycle_request_id;
        ALTER TABLE lifecycle_requests
            DROP CONSTRAINT uq_lifecycle_requests_scope_id;
        ALTER TABLE lifecycle_requests
            DROP CONSTRAINT fk_lifecycle_requests_source_version;
        ALTER TABLE lifecycle_requests
            DROP COLUMN source_document_version_id;
        DROP INDEX ix_document_versions_reprocessed_from;
        ALTER TABLE document_versions
            DROP CONSTRAINT ck_document_versions_reprocessed_not_self;
        ALTER TABLE document_versions
            DROP CONSTRAINT fk_document_versions_reprocessed_from;
        ALTER TABLE document_versions
            DROP COLUMN reprocessed_from_version_id;
        """
    )
