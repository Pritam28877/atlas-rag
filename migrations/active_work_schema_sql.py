"""Durable search-cleanup and scheduler schema for active-work guards."""

DURABLE_SEARCH_SCHEMA = """
ALTER TABLE ingestion_jobs
    ADD COLUMN publication_target_name VARCHAR(255),
    ADD COLUMN publication_target_version VARCHAR(128),
    ADD COLUMN publication_target_attempt INTEGER,
    ADD CONSTRAINT ck_ingestion_jobs_publication_target CHECK (
        (publication_target_name IS NULL) =
        (publication_target_version IS NULL)
        AND (publication_target_name IS NULL) =
            (publication_target_attempt IS NULL)
        AND (
            publication_target_attempt IS NULL
            OR publication_target_attempt > 0
        )
    );

ALTER TABLE artifacts
    ADD COLUMN search_target_name VARCHAR(255),
    ADD COLUMN search_target_version VARCHAR(128),
    ADD CONSTRAINT ck_artifacts_search_target CHECK (
        (search_target_name IS NULL) = (search_target_version IS NULL)
        AND (
            search_target_name IS NULL
            OR (artifact_type = 'index_manifest' AND generator_name = 'opensearch')
        )
    );

ALTER TABLE index_publications
    ADD COLUMN publication_job_id UUID,
    ADD COLUMN publication_attempt INTEGER,
    ADD COLUMN activated_at TIMESTAMPTZ,
    ADD CONSTRAINT fk_index_publications_job FOREIGN KEY (
        tenant_id, collection_id, publication_job_id
    ) REFERENCES ingestion_jobs(tenant_id, collection_id, id) ON DELETE CASCADE,
    ADD CONSTRAINT ck_index_publications_attempt CHECK (
        (publication_job_id IS NULL) = (publication_attempt IS NULL)
        AND (publication_attempt IS NULL OR publication_attempt > 0)
    ),
    ADD CONSTRAINT ck_index_publications_activation CHECK (
        activated_at IS NULL OR activated_at >= published_at
    );

CREATE TABLE search_cleanup_entries (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    collection_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    activation_version_id UUID,
    publication_job_id UUID,
    publication_attempt INTEGER,
    target_name VARCHAR(255) NOT NULL,
    target_version VARCHAR(128) NOT NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_owner VARCHAR(255),
    lease_expires_at TIMESTAMPTZ,
    last_reason_code VARCHAR(128),
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, collection_id, document_version_id)
        REFERENCES document_versions(tenant_id, collection_id, id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, collection_id, activation_version_id)
        REFERENCES document_versions(tenant_id, collection_id, id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, collection_id, publication_job_id)
        REFERENCES ingestion_jobs(tenant_id, collection_id, id)
        ON DELETE CASCADE,
    CONSTRAINT ck_search_cleanup_state CHECK (
        state IN ('pending', 'running', 'deleted', 'failed')
    ),
    CONSTRAINT ck_search_cleanup_attempts CHECK (attempt_count >= 0),
    CONSTRAINT ck_search_cleanup_publication_attempt CHECK (
        (publication_job_id IS NULL) = (publication_attempt IS NULL)
        AND (publication_attempt IS NULL OR publication_attempt > 0)
    ),
    CONSTRAINT ck_search_cleanup_lease CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CONSTRAINT ck_search_cleanup_deleted CHECK (
        (state = 'deleted' AND deleted_at IS NOT NULL)
        OR (state <> 'deleted' AND deleted_at IS NULL)
    ),
    CONSTRAINT uq_search_cleanup_target UNIQUE NULLS NOT DISTINCT (
        tenant_id, collection_id, document_version_id,
        activation_version_id, publication_job_id, publication_attempt,
        target_name, target_version
    )
);
CREATE INDEX ix_search_cleanup_pending
    ON search_cleanup_entries (state, updated_at, id)
    WHERE state IN ('pending', 'running');
CREATE INDEX ix_search_cleanup_activation
    ON search_cleanup_entries (
        tenant_id, collection_id, activation_version_id, state
    ) WHERE activation_version_id IS NOT NULL;

CREATE TABLE service_heartbeats (
    service_name VARCHAR(64) PRIMARY KEY,
    updated_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE version_transition_events
    DROP CONSTRAINT ck_version_transition_operation;
ALTER TABLE version_transition_events
    ADD CONSTRAINT ck_version_transition_operation CHECK (
        operation IN ('advance', 'retry', 'reprocess', 'cancel',
            'supersede', 'delete', 'reconcile')
    );
"""
