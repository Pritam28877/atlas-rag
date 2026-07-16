"""Create tenant-scoped document catalog core.

Revision ID: 20260716_02
Revises: 20260713_01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260716_02"
down_revision: str | Sequence[str] | None = "20260713_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tenants (
            id UUID PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_tenants_name UNIQUE (name)
        );

        CREATE TABLE collections (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            name VARCHAR(200) NOT NULL,
            retention_days INTEGER NOT NULL DEFAULT 0,
            upload_max_bytes BIGINT NOT NULL,
            document_quota BIGINT NOT NULL,
            storage_quota_bytes BIGINT NOT NULL,
            legal_hold BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_collections_retention CHECK (retention_days >= 0),
            CONSTRAINT ck_collections_upload_limit CHECK (upload_max_bytes > 0),
            CONSTRAINT ck_collections_document_quota CHECK (document_quota > 0),
            CONSTRAINT ck_collections_storage_quota CHECK (storage_quota_bytes > 0),
            CONSTRAINT uq_collections_tenant_id UNIQUE (tenant_id, id),
            CONSTRAINT uq_collections_tenant_name UNIQUE (tenant_id, name)
        );
        CREATE INDEX ix_collections_tenant_created
            ON collections (tenant_id, created_at, id);

        CREATE TABLE collection_memberships (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL,
            collection_id UUID NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            role VARCHAR(16) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, collection_id)
                REFERENCES collections(tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT ck_collection_memberships_role
                CHECK (role IN ('owner', 'editor', 'viewer')),
            CONSTRAINT uq_collection_memberships_subject
                UNIQUE (tenant_id, collection_id, principal_subject)
        );
        CREATE INDEX ix_collection_memberships_principal
            ON collection_memberships (tenant_id, principal_subject, collection_id);

        CREATE TABLE source_documents (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL,
            collection_id UUID NOT NULL,
            source_key VARCHAR(255) NOT NULL,
            display_name VARCHAR(500) NOT NULL,
            external_source_type VARCHAR(64),
            external_source_ref VARCHAR(1000),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, collection_id)
                REFERENCES collections(tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT ck_source_documents_external_pair CHECK (
                (external_source_type IS NULL) = (external_source_ref IS NULL)
            ),
            CONSTRAINT uq_source_documents_scope_id
                UNIQUE (tenant_id, collection_id, id),
            CONSTRAINT uq_source_documents_source_key
                UNIQUE (tenant_id, collection_id, source_key)
        );
        CREATE INDEX ix_source_documents_scope_created
            ON source_documents (tenant_id, collection_id, created_at, id);
        CREATE UNIQUE INDEX uq_source_documents_external_ref
            ON source_documents (
                tenant_id, collection_id, external_source_type, external_source_ref
            )
            WHERE external_source_type IS NOT NULL;

        CREATE TABLE document_versions (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL,
            collection_id UUID NOT NULL,
            document_id UUID NOT NULL,
            version_number INTEGER NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL,
            content_sha256 CHAR(64) NOT NULL,
            size_bytes BIGINT NOT NULL,
            page_count INTEGER,
            detected_mime VARCHAR(127),
            object_key VARCHAR(1024) NOT NULL,
            source_uri_policy VARCHAR(64) NOT NULL DEFAULT 'direct_upload_only',
            pipeline_profile VARCHAR(128) NOT NULL,
            state VARCHAR(32) NOT NULL DEFAULT 'received',
            state_revision BIGINT NOT NULL DEFAULT 0,
            progress_completed INTEGER NOT NULL DEFAULT 0,
            progress_total INTEGER NOT NULL DEFAULT 0,
            terminal_reason_code VARCHAR(128),
            canonical_version_id UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, collection_id, document_id)
                REFERENCES source_documents(tenant_id, collection_id, id)
                ON DELETE RESTRICT,
            FOREIGN KEY (tenant_id, collection_id, canonical_version_id)
                REFERENCES document_versions(tenant_id, collection_id, id)
                ON DELETE RESTRICT,
            CONSTRAINT ck_document_versions_number CHECK (version_number > 0),
            CONSTRAINT ck_document_versions_sha256
                CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_document_versions_size CHECK (size_bytes > 0),
            CONSTRAINT ck_document_versions_pages
                CHECK (page_count IS NULL OR page_count > 0),
            CONSTRAINT ck_document_versions_progress CHECK (
                progress_completed >= 0 AND progress_total >= 0
                AND (progress_total = 0 OR progress_completed <= progress_total)
            ),
            CONSTRAINT ck_document_versions_state CHECK (state IN (
                'received', 'validating', 'queued', 'parsing', 'ocr',
                'normalizing', 'chunking', 'embedding', 'indexing', 'ready',
                'ready_with_warnings', 'deduplicated', 'rejected', 'failed',
                'quarantined', 'superseded', 'cancelled', 'deleted'
            )),
            CONSTRAINT ck_document_versions_reason CHECK (
                (state IN ('deduplicated', 'rejected', 'failed', 'quarantined')
                    AND terminal_reason_code IS NOT NULL)
                OR state NOT IN (
                    'deduplicated', 'rejected', 'failed', 'quarantined'
                )
            ),
            CONSTRAINT ck_document_versions_canonical CHECK (
                (state = 'deduplicated' AND canonical_version_id IS NOT NULL)
                OR (state <> 'deduplicated' AND canonical_version_id IS NULL)
            ),
            CONSTRAINT ck_document_versions_not_self_canonical
                CHECK (canonical_version_id IS NULL OR canonical_version_id <> id),
            CONSTRAINT ck_document_versions_reason_terminal CHECK (
                terminal_reason_code IS NULL OR state IN (
                    'ready', 'ready_with_warnings', 'deduplicated', 'rejected',
                    'failed', 'quarantined', 'superseded', 'cancelled', 'deleted'
                )
            ),
            CONSTRAINT uq_document_versions_scope_id
                UNIQUE (tenant_id, collection_id, id),
            CONSTRAINT uq_document_versions_number
                UNIQUE (tenant_id, collection_id, document_id, version_number),
            CONSTRAINT uq_document_versions_idempotency
                UNIQUE (tenant_id, collection_id, idempotency_key)
        );
        CREATE INDEX ix_document_versions_scope_state
            ON document_versions (tenant_id, collection_id, state, created_at, id);
        CREATE INDEX ix_document_versions_tenant_hash
            ON document_versions (tenant_id, content_sha256, pipeline_profile);

        CREATE TABLE artifacts (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL,
            collection_id UUID NOT NULL,
            document_version_id UUID NOT NULL,
            source_artifact_id UUID,
            artifact_type VARCHAR(32) NOT NULL,
            object_key VARCHAR(1024) NOT NULL,
            generator_name VARCHAR(128) NOT NULL,
            generator_version VARCHAR(128) NOT NULL,
            checksum_sha256 CHAR(64) NOT NULL,
            size_bytes BIGINT NOT NULL,
            retention_until TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, collection_id, document_version_id)
                REFERENCES document_versions(tenant_id, collection_id, id)
                ON DELETE CASCADE,
            FOREIGN KEY (
                tenant_id, collection_id, document_version_id, source_artifact_id
            ) REFERENCES artifacts(
                tenant_id, collection_id, document_version_id, id
            ) ON DELETE RESTRICT,
            CONSTRAINT ck_artifacts_type CHECK (artifact_type IN (
                'original_pdf', 'inspection_report', 'extracted_pages',
                'layout_blocks', 'ocr_result', 'normalized_document',
                'chunk_manifest', 'index_manifest'
            )),
            CONSTRAINT ck_artifacts_checksum
                CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_artifacts_size CHECK (size_bytes >= 0),
            CONSTRAINT uq_artifacts_scope_id UNIQUE (tenant_id, collection_id, id),
            CONSTRAINT uq_artifacts_version_id UNIQUE (
                tenant_id, collection_id, document_version_id, id
            ),
            CONSTRAINT uq_artifacts_output UNIQUE (
                tenant_id, collection_id, document_version_id, artifact_type,
                generator_name, generator_version, checksum_sha256
            ),
            CONSTRAINT uq_artifacts_object_key UNIQUE (object_key)
        );
        CREATE INDEX ix_artifacts_version_type
            ON artifacts (
                tenant_id, collection_id, document_version_id, artifact_type, created_at
            );

        CREATE FUNCTION protect_artifact_provenance() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.id, NEW.tenant_id, NEW.collection_id, NEW.document_version_id,
                NEW.source_artifact_id, NEW.artifact_type, NEW.object_key,
                NEW.generator_name, NEW.generator_version, NEW.checksum_sha256,
                NEW.size_bytes, NEW.created_at)
                IS DISTINCT FROM
               (OLD.id, OLD.tenant_id, OLD.collection_id, OLD.document_version_id,
                OLD.source_artifact_id, OLD.artifact_type, OLD.object_key,
                OLD.generator_name, OLD.generator_version, OLD.checksum_sha256,
                OLD.size_bytes, OLD.created_at) THEN
                RAISE EXCEPTION 'artifact provenance is immutable';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER artifacts_immutable
            BEFORE UPDATE ON artifacts
            FOR EACH ROW EXECUTE FUNCTION protect_artifact_provenance();

        CREATE FUNCTION protect_document_version_identity() RETURNS trigger
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
        CREATE TRIGGER document_versions_identity_immutable
            BEFORE UPDATE ON document_versions
            FOR EACH ROW EXECUTE FUNCTION protect_document_version_identity();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE artifacts;
        DROP FUNCTION protect_artifact_provenance();
        DROP TABLE document_versions;
        DROP FUNCTION protect_document_version_identity();
        DROP TABLE source_documents;
        DROP TABLE collection_memberships;
        DROP TABLE collections;
        DROP TABLE tenants;
        """
    )
