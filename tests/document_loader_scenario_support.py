"""Durable-state probes for the document-loader end-to-end scenario."""

from uuid import UUID, uuid4

from sqlalchemy import text

from app.workers.celery_app import PipelineTask


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[PipelineTask, UUID, UUID, UUID]] = []

    async def dispatch(
        self,
        task: PipelineTask,
        tenant_id: UUID,
        version_id: UUID,
        job_id: UUID,
    ) -> None:
        self.calls.append((task, tenant_id, version_id, job_id))

    def job_id(self, task: PipelineTask) -> UUID:
        matching_jobs = [call[3] for call in self.calls if call[0] is task]
        assert len(matching_jobs) == 1
        return matching_jobs[0]


class FailingDispatcher:
    async def dispatch(
        self,
        task: PipelineTask,
        tenant_id: UUID,
        version_id: UUID,
        job_id: UUID,
    ) -> None:
        raise RuntimeError("simulated broker outage")


def assert_durable_handoff(engine, predecessor_id: UUID, successor_id: UUID) -> None:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT stage, state, attempt_count FROM ingestion_jobs
                WHERE id IN (:predecessor_id, :successor_id)
                ORDER BY stage
                """
            ),
            {
                "predecessor_id": predecessor_id,
                "successor_id": successor_id,
            },
        ).mappings().all()
    assert {row["stage"]: row["state"] for row in rows} == {
        "chunk": "retry_scheduled",
        "preflight": "succeeded",
    }
    assert {row["stage"]: row["attempt_count"] for row in rows} == {
        "chunk": 0,
        "preflight": 1,
    }


def assert_ready_publication(engine, version_id: UUID) -> None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT version.state, chunk.page_start, chunk.page_end,
                       count(DISTINCT embedding.id) AS embeddings,
                       count(DISTINCT publication.id) AS publications
                FROM document_versions version
                JOIN chunks chunk ON chunk.document_version_id = version.id
                LEFT JOIN chunk_embeddings embedding
                  ON embedding.chunk_id = chunk.id
                LEFT JOIN index_publications publication
                  ON publication.chunk_id = chunk.id
                 AND publication.deleted_at IS NULL
                WHERE version.id = :version_id
                GROUP BY version.state, chunk.page_start, chunk.page_end
                """
            ),
            {"version_id": version_id},
        ).one()
    assert row.state == "ready"
    assert row.page_start == row.page_end == 1
    assert row.embeddings >= 1
    assert row.publications == row.embeddings * 2


def seed_stale_index_job(engine, version_id: UUID) -> UUID:
    stale_job_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ingestion_jobs (
                    id, tenant_id, collection_id, document_version_id,
                    stage, generation, max_attempts, created_at, updated_at
                ) SELECT :id, tenant_id, collection_id, id,
                    'index', 99, 3, now() - interval '1 hour',
                    now() - interval '1 hour'
                FROM document_versions WHERE id = :version_id
                """
            ),
            {"id": stale_job_id, "version_id": version_id},
        )
    return stale_job_id


def artifact_keys(engine, tenant_id: UUID, version_id: UUID) -> tuple[str, ...]:
    with engine.connect() as connection:
        return tuple(
            connection.scalars(
                text(
                    """
                    SELECT object_key FROM artifacts
                    WHERE tenant_id = :tenant_id
                      AND document_version_id = :version_id
                    """
                ),
                {"tenant_id": tenant_id, "version_id": version_id},
            )
        )


def assert_legal_hold_defers_cleanup(
    engine,
    tenant_id: UUID,
    version_id: UUID,
) -> None:
    with engine.connect() as connection:
        deleted_state = connection.scalar(
            text("SELECT state FROM document_versions WHERE id = :id"),
            {"id": version_id},
        )
        remaining_artifacts = connection.scalar(
            text(
                "SELECT count(*) FROM artifacts "
                "WHERE tenant_id = :tenant_id AND document_version_id = :version_id"
            ),
            {"tenant_id": tenant_id, "version_id": version_id},
        )
        pending_cleanup = connection.scalar(
            text(
                "SELECT count(*) FROM object_cleanup_entries "
                "WHERE tenant_id = :tenant_id AND document_version_id = :version_id "
                "AND state = 'pending'"
            ),
            {"tenant_id": tenant_id, "version_id": version_id},
        )
    assert deleted_state == "deleted"
    assert int(str(remaining_artifacts)) > 0
    assert int(str(pending_cleanup)) > 0
