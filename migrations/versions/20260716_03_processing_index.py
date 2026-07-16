"""Create processing, provenance, and publication catalog records.

Revision ID: 20260716_03
Revises: 20260716_02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260716_03"
down_revision: str | Sequence[str] | None = "20260716_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ingestion_jobs (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL,
            collection_id UUID NOT NULL,
            document_version_id UUID NOT NULL,
            stage VARCHAR(32) NOT NULL,
            state VARCHAR(24) NOT NULL DEFAULT 'pending',
            generation INTEGER NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL,
            lease_owner VARCHAR(255),
            lease_expires_at TIMESTAMPTZ,
            heartbeat_at TIMESTAMPTZ,
            retry_at TIMESTAMPTZ,
            terminal_reason_code VARCHAR(128),
            sanitized_context JSONB NOT NULL DEFAULT '{}'::jsonb,
            dead_letter_ref VARCHAR(500),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, collection_id, document_version_id)
                REFERENCES document_versions(tenant_id, collection_id, id)
                ON DELETE CASCADE,
            CONSTRAINT ck_ingestion_jobs_stage CHECK (stage IN (
                'intake', 'preflight', 'native_parse', 'ocr', 'normalize',
                'chunk', 'embed', 'index', 'delete', 'reprocess'
            )),
            CONSTRAINT ck_ingestion_jobs_state CHECK (state IN (
                'pending', 'leased', 'running', 'retry_scheduled', 'succeeded',
                'failed', 'cancelled', 'dead_lettered'
            )),
            CONSTRAINT ck_ingestion_jobs_generation CHECK (generation >= 0),
            CONSTRAINT ck_ingestion_jobs_attempts CHECK (
                attempt_count >= 0 AND max_attempts > 0
                AND attempt_count <= max_attempts
            ),
            CONSTRAINT ck_ingestion_jobs_lease CHECK (
                (lease_owner IS NULL AND lease_expires_at IS NULL)
                OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
            ),
            CONSTRAINT ck_ingestion_jobs_context
                CHECK (jsonb_typeof(sanitized_context) = 'object'),
            CONSTRAINT uq_ingestion_jobs_scope_id
                UNIQUE (tenant_id, collection_id, id),
            CONSTRAINT uq_ingestion_jobs_delivery
                UNIQUE (
                    tenant_id, collection_id, document_version_id, stage, generation
                )
        );
        CREATE INDEX ix_ingestion_jobs_claim
            ON ingestion_jobs (stage, state, retry_at, lease_expires_at, created_at)
            WHERE state IN ('pending', 'leased', 'retry_scheduled');
        CREATE INDEX ix_ingestion_jobs_version
            ON ingestion_jobs (
                tenant_id, collection_id, document_version_id, created_at, id
            );

        CREATE TABLE job_attempts (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL,
            collection_id UUID NOT NULL,
            job_id UUID NOT NULL,
            attempt_number INTEGER NOT NULL,
            worker_id VARCHAR(255) NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            heartbeat_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            outcome VARCHAR(24),
            error_class VARCHAR(32),
            reason_code VARCHAR(128),
            sanitized_context JSONB NOT NULL DEFAULT '{}'::jsonb,
            FOREIGN KEY (tenant_id, collection_id, job_id)
                REFERENCES ingestion_jobs(tenant_id, collection_id, id)
                ON DELETE CASCADE,
            CONSTRAINT ck_job_attempts_number CHECK (attempt_number > 0),
            CONSTRAINT ck_job_attempts_times CHECK (
                finished_at IS NULL OR finished_at >= started_at
            ),
            CONSTRAINT ck_job_attempts_outcome CHECK (
                outcome IS NULL OR outcome IN (
                    'succeeded', 'retryable_failure', 'permanent_failure',
                    'cancelled', 'lease_expired'
                )
            ),
            CONSTRAINT ck_job_attempts_error_class CHECK (
                error_class IS NULL OR error_class IN (
                    'dependency', 'invalid_input', 'resource_limit',
                    'security', 'internal'
                )
            ),
            CONSTRAINT ck_job_attempts_context
                CHECK (jsonb_typeof(sanitized_context) = 'object'),
            CONSTRAINT uq_job_attempts_number
                UNIQUE (tenant_id, collection_id, job_id, attempt_number)
        );
        CREATE INDEX ix_job_attempts_job
            ON job_attempts (tenant_id, collection_id, job_id, attempt_number);

        CREATE TABLE version_transition_events (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL,
            collection_id UUID NOT NULL,
            document_version_id UUID NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL,
            from_state VARCHAR(32) NOT NULL,
            to_state VARCHAR(32) NOT NULL,
            from_revision BIGINT NOT NULL,
            to_revision BIGINT NOT NULL,
            operation VARCHAR(24) NOT NULL,
            actor_type VARCHAR(32) NOT NULL,
            actor_id VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, collection_id, document_version_id)
                REFERENCES document_versions(tenant_id, collection_id, id)
                ON DELETE CASCADE,
            CONSTRAINT ck_version_transition_revision CHECK (
                from_revision >= 0 AND to_revision = from_revision + 1
            ),
            CONSTRAINT ck_version_transition_operation CHECK (
                operation IN ('advance', 'retry', 'reprocess', 'cancel',
                    'supersede', 'delete')
            ),
            CONSTRAINT ck_version_transition_states CHECK (
                from_state IN (
                    'received', 'validating', 'queued', 'parsing', 'ocr',
                    'normalizing', 'chunking', 'embedding', 'indexing', 'ready',
                    'ready_with_warnings', 'deduplicated', 'rejected', 'failed',
                    'quarantined', 'superseded', 'cancelled', 'deleted'
                ) AND to_state IN (
                    'received', 'validating', 'queued', 'parsing', 'ocr',
                    'normalizing', 'chunking', 'embedding', 'indexing', 'ready',
                    'ready_with_warnings', 'deduplicated', 'rejected', 'failed',
                    'quarantined', 'superseded', 'cancelled', 'deleted'
                )
            ),
            CONSTRAINT uq_version_transition_idempotency UNIQUE (
                tenant_id, collection_id, document_version_id, idempotency_key
            ),
            CONSTRAINT uq_version_transition_revision UNIQUE (
                tenant_id, collection_id, document_version_id, to_revision
            )
        );

        CREATE TABLE chunks (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL,
            collection_id UUID NOT NULL,
            document_version_id UUID NOT NULL,
            source_artifact_id UUID NOT NULL,
            ordinal INTEGER NOT NULL,
            page_start INTEGER NOT NULL,
            page_end INTEGER NOT NULL,
            content_sha256 CHAR(64) NOT NULL,
            content_object_key VARCHAR(1024) NOT NULL,
            token_count INTEGER NOT NULL,
            section_path JSONB NOT NULL DEFAULT '[]'::jsonb,
            block_bounds JSONB NOT NULL DEFAULT '[]'::jsonb,
            chunker_name VARCHAR(128) NOT NULL,
            chunker_version VARCHAR(128) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, collection_id, document_version_id)
                REFERENCES document_versions(tenant_id, collection_id, id)
                ON DELETE CASCADE,
            FOREIGN KEY (
                tenant_id, collection_id, document_version_id, source_artifact_id
            ) REFERENCES artifacts(
                tenant_id, collection_id, document_version_id, id
            ) ON DELETE RESTRICT,
            CONSTRAINT ck_chunks_ordinal CHECK (ordinal >= 0),
            CONSTRAINT ck_chunks_pages CHECK (
                page_start > 0 AND page_end >= page_start
            ),
            CONSTRAINT ck_chunks_checksum CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_chunks_tokens CHECK (token_count > 0),
            CONSTRAINT ck_chunks_section_path CHECK (
                jsonb_typeof(section_path) = 'array'
            ),
            CONSTRAINT ck_chunks_block_bounds CHECK (
                jsonb_typeof(block_bounds) = 'array'
            ),
            CONSTRAINT uq_chunks_scope_id UNIQUE (tenant_id, collection_id, id),
            CONSTRAINT uq_chunks_version_id UNIQUE (
                tenant_id, collection_id, document_version_id, id
            ),
            CONSTRAINT uq_chunks_ordinal UNIQUE (
                tenant_id, collection_id, document_version_id,
                chunker_name, chunker_version, ordinal
            )
        );
        CREATE INDEX ix_chunks_version_pages
            ON chunks (
                tenant_id, collection_id, document_version_id, page_start, ordinal
            );

        CREATE TABLE chunk_embeddings (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL,
            collection_id UUID NOT NULL,
            document_version_id UUID NOT NULL,
            chunk_id UUID NOT NULL,
            provider VARCHAR(128) NOT NULL,
            model_name VARCHAR(255) NOT NULL,
            model_version VARCHAR(128) NOT NULL,
            dimensions INTEGER NOT NULL,
            vector_object_key VARCHAR(1024) NOT NULL,
            vector_checksum_sha256 CHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, collection_id, document_version_id)
                REFERENCES document_versions(tenant_id, collection_id, id)
                ON DELETE CASCADE,
            FOREIGN KEY (
                tenant_id, collection_id, document_version_id, chunk_id
            ) REFERENCES chunks(
                tenant_id, collection_id, document_version_id, id
            ) ON DELETE CASCADE,
            CONSTRAINT ck_chunk_embeddings_dimensions CHECK (dimensions > 0),
            CONSTRAINT ck_chunk_embeddings_checksum
                CHECK (vector_checksum_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT uq_chunk_embeddings_model UNIQUE (
                tenant_id, collection_id, chunk_id,
                provider, model_name, model_version
            )
        );
        CREATE INDEX ix_chunk_embeddings_version
            ON chunk_embeddings (
                tenant_id, collection_id, document_version_id, model_name, id
            );

        CREATE TABLE index_publications (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL,
            collection_id UUID NOT NULL,
            document_version_id UUID NOT NULL,
            chunk_id UUID NOT NULL,
            publication_kind VARCHAR(16) NOT NULL,
            target_name VARCHAR(255) NOT NULL,
            target_version VARCHAR(128) NOT NULL,
            external_record_id VARCHAR(500) NOT NULL,
            published_at TIMESTAMPTZ NOT NULL,
            deleted_at TIMESTAMPTZ,
            FOREIGN KEY (tenant_id, collection_id, document_version_id)
                REFERENCES document_versions(tenant_id, collection_id, id)
                ON DELETE CASCADE,
            FOREIGN KEY (
                tenant_id, collection_id, document_version_id, chunk_id
            ) REFERENCES chunks(
                tenant_id, collection_id, document_version_id, id
            ) ON DELETE CASCADE,
            CONSTRAINT ck_index_publications_kind
                CHECK (publication_kind IN ('lexical', 'vector')),
            CONSTRAINT ck_index_publications_deleted CHECK (
                deleted_at IS NULL OR deleted_at >= published_at
            ),
            CONSTRAINT uq_index_publications_target UNIQUE (
                tenant_id, collection_id, chunk_id, publication_kind,
                target_name, target_version
            ),
            CONSTRAINT uq_index_publications_external UNIQUE (
                target_name, target_version, external_record_id
            )
        );
        CREATE INDEX ix_index_publications_version
            ON index_publications (
                tenant_id, collection_id, document_version_id,
                publication_kind, deleted_at
            );

        CREATE TABLE lifecycle_requests (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL,
            collection_id UUID NOT NULL,
            document_version_id UUID NOT NULL,
            request_type VARCHAR(16) NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL,
            requested_by VARCHAR(255) NOT NULL,
            state VARCHAR(16) NOT NULL DEFAULT 'pending',
            requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            FOREIGN KEY (tenant_id, collection_id, document_version_id)
                REFERENCES document_versions(tenant_id, collection_id, id)
                ON DELETE RESTRICT,
            CONSTRAINT ck_lifecycle_requests_type
                CHECK (request_type IN ('retry', 'reprocess', 'cancel', 'delete')),
            CONSTRAINT ck_lifecycle_requests_state
                CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
            CONSTRAINT ck_lifecycle_requests_completed CHECK (
                completed_at IS NULL OR completed_at >= requested_at
            ),
            CONSTRAINT uq_lifecycle_requests_idempotency
                UNIQUE (tenant_id, collection_id, idempotency_key)
        );
        CREATE INDEX ix_lifecycle_requests_pending
            ON lifecycle_requests (
                tenant_id, collection_id, state, requested_at, id
            );

        CREATE FUNCTION enforce_ready_publication() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.state IN ('ready', 'ready_with_warnings') AND (
                NOT EXISTS (
                    SELECT 1 FROM artifacts
                    WHERE tenant_id = NEW.tenant_id
                        AND collection_id = NEW.collection_id
                        AND document_version_id = NEW.id
                        AND artifact_type = 'chunk_manifest'
                )
                OR NOT EXISTS (
                    SELECT 1 FROM artifacts
                    WHERE tenant_id = NEW.tenant_id
                        AND collection_id = NEW.collection_id
                        AND document_version_id = NEW.id
                        AND artifact_type = 'index_manifest'
                )
                OR
                NOT EXISTS (
                    SELECT 1 FROM chunks
                    WHERE tenant_id = NEW.tenant_id
                        AND collection_id = NEW.collection_id
                        AND document_version_id = NEW.id
                )
                OR EXISTS (
                    SELECT 1 FROM chunks AS catalog_chunk
                    WHERE catalog_chunk.tenant_id = NEW.tenant_id
                        AND catalog_chunk.collection_id = NEW.collection_id
                        AND catalog_chunk.document_version_id = NEW.id
                        AND (
                            NOT EXISTS (
                                SELECT 1 FROM chunk_embeddings AS embedding
                                WHERE embedding.tenant_id = catalog_chunk.tenant_id
                                    AND embedding.collection_id =
                                        catalog_chunk.collection_id
                                    AND embedding.chunk_id = catalog_chunk.id
                            )
                            OR NOT EXISTS (
                                SELECT 1 FROM index_publications AS publication
                                WHERE publication.tenant_id = catalog_chunk.tenant_id
                                    AND publication.collection_id =
                                        catalog_chunk.collection_id
                                    AND publication.chunk_id = catalog_chunk.id
                                    AND publication.publication_kind = 'lexical'
                                    AND publication.deleted_at IS NULL
                            )
                            OR NOT EXISTS (
                                SELECT 1 FROM index_publications AS publication
                                WHERE publication.tenant_id = catalog_chunk.tenant_id
                                    AND publication.collection_id =
                                        catalog_chunk.collection_id
                                    AND publication.chunk_id = catalog_chunk.id
                                    AND publication.publication_kind = 'vector'
                                    AND publication.deleted_at IS NULL
                            )
                        )
                )
            ) THEN
                RAISE EXCEPTION 'ready state requires complete publication evidence';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER document_versions_ready_publication
            BEFORE INSERT OR UPDATE OF state ON document_versions
            FOR EACH ROW EXECUTE FUNCTION enforce_ready_publication();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER document_versions_ready_publication ON document_versions;
        DROP FUNCTION enforce_ready_publication();
        DROP TABLE lifecycle_requests;
        DROP TABLE index_publications;
        DROP TABLE chunk_embeddings;
        DROP TABLE chunks;
        DROP TABLE version_transition_events;
        DROP TABLE job_attempts;
        DROP TABLE ingestion_jobs;
        """
    )
