"""Forward and rollback trigger functions for active-work guards."""

TARGETED_ARTIFACT_PROVENANCE_FUNCTION = """
CREATE OR REPLACE FUNCTION protect_artifact_provenance() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.collection_id, NEW.document_version_id,
        NEW.source_artifact_id, NEW.artifact_type, NEW.object_key,
        NEW.generator_name, NEW.generator_version, NEW.checksum_sha256,
        NEW.size_bytes, NEW.search_target_name, NEW.search_target_version,
        NEW.created_at)
        IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.collection_id, OLD.document_version_id,
        OLD.source_artifact_id, OLD.artifact_type, OLD.object_key,
        OLD.generator_name, OLD.generator_version, OLD.checksum_sha256,
        OLD.size_bytes, OLD.search_target_name, OLD.search_target_version,
        OLD.created_at) THEN
        RAISE EXCEPTION 'artifact provenance is immutable';
    END IF;
    RETURN NEW;
END;
$$;
"""


LEGACY_ARTIFACT_PROVENANCE_FUNCTION = """
CREATE OR REPLACE FUNCTION protect_artifact_provenance() RETURNS trigger
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
"""


STRICT_READY_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_ready_publication() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state IN ('ready', 'ready_with_warnings') AND (
        NOT EXISTS (
            SELECT 1 FROM artifacts artifact
            WHERE artifact.tenant_id = NEW.tenant_id
              AND artifact.collection_id = NEW.collection_id
              AND artifact.document_version_id = NEW.id
              AND artifact.artifact_type = 'chunk_manifest'
        )
        OR NOT EXISTS (
            SELECT 1 FROM chunks chunk
            WHERE chunk.tenant_id = NEW.tenant_id
              AND chunk.collection_id = NEW.collection_id
              AND chunk.document_version_id = NEW.id
        )
        OR EXISTS (
            SELECT 1 FROM chunks chunk
            WHERE chunk.tenant_id = NEW.tenant_id
              AND chunk.collection_id = NEW.collection_id
              AND chunk.document_version_id = NEW.id
              AND NOT EXISTS (
                  SELECT 1 FROM chunk_embeddings embedding
                  WHERE embedding.tenant_id = chunk.tenant_id
                    AND embedding.collection_id = chunk.collection_id
                    AND embedding.document_version_id = chunk.document_version_id
                    AND embedding.chunk_id = chunk.id
              )
        )
        OR NOT EXISTS (
            SELECT publication.target_name, publication.target_version,
                   publication.publication_job_id,
                   publication.publication_attempt
            FROM index_publications publication
            WHERE publication.tenant_id = NEW.tenant_id
              AND publication.collection_id = NEW.collection_id
              AND publication.document_version_id = NEW.id
              AND publication.deleted_at IS NULL
              AND EXISTS (
                  SELECT 1 FROM artifacts manifest
                  WHERE manifest.tenant_id = NEW.tenant_id
                    AND manifest.collection_id = NEW.collection_id
                    AND manifest.document_version_id = NEW.id
                    AND manifest.artifact_type = 'index_manifest'
                    AND manifest.generator_name = 'opensearch'
                    AND manifest.search_target_name = publication.target_name
                    AND manifest.search_target_version =
                        publication.target_version
              )
            GROUP BY publication.target_name, publication.target_version,
                     publication.publication_job_id,
                     publication.publication_attempt
            HAVING count(DISTINCT publication.chunk_id) FILTER (
                    WHERE publication.publication_kind = 'lexical'
                  ) = (
                    SELECT count(*) FROM chunks chunk
                    WHERE chunk.tenant_id = NEW.tenant_id
                      AND chunk.collection_id = NEW.collection_id
                      AND chunk.document_version_id = NEW.id
                  )
              AND count(DISTINCT publication.chunk_id) FILTER (
                    WHERE publication.publication_kind = 'vector'
                  ) = (
                    SELECT count(*) FROM chunks chunk
                    WHERE chunk.tenant_id = NEW.tenant_id
                      AND chunk.collection_id = NEW.collection_id
                      AND chunk.document_version_id = NEW.id
                  )
        )
    ) THEN
        RAISE EXCEPTION 'ready state requires complete publication evidence';
    END IF;
    RETURN NEW;
END;
$$;
"""


LEGACY_READY_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_ready_publication() RETURNS trigger
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
        OR NOT EXISTS (
            SELECT 1 FROM chunks
            WHERE tenant_id = NEW.tenant_id
              AND collection_id = NEW.collection_id
              AND document_version_id = NEW.id
        )
        OR EXISTS (
            SELECT 1 FROM chunks catalog_chunk
            WHERE catalog_chunk.tenant_id = NEW.tenant_id
              AND catalog_chunk.collection_id = NEW.collection_id
              AND catalog_chunk.document_version_id = NEW.id
              AND (
                NOT EXISTS (
                    SELECT 1 FROM chunk_embeddings embedding
                    WHERE embedding.tenant_id = catalog_chunk.tenant_id
                      AND embedding.collection_id = catalog_chunk.collection_id
                      AND embedding.chunk_id = catalog_chunk.id
                )
                OR NOT EXISTS (
                    SELECT 1 FROM index_publications publication
                    WHERE publication.tenant_id = catalog_chunk.tenant_id
                      AND publication.collection_id = catalog_chunk.collection_id
                      AND publication.chunk_id = catalog_chunk.id
                      AND publication.publication_kind = 'lexical'
                      AND publication.deleted_at IS NULL
                )
                OR NOT EXISTS (
                    SELECT 1 FROM index_publications publication
                    WHERE publication.tenant_id = catalog_chunk.tenant_id
                      AND publication.collection_id = catalog_chunk.collection_id
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
"""
