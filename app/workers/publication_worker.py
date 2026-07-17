"""CLI entrypoint for bounded chunk, embedding, and index publication."""

import asyncio
from functools import lru_cache
from typing import cast

from app.core.config import get_settings
from app.core.database import Database
from app.core.storage import ObjectStorage
from app.services.catalog.service import CeleryJobDispatcher
from app.services.ingestion.embedding_provider import FastEmbedProvider
from app.services.ingestion.errors import RetryableIngestionError
from app.services.ingestion.publication_service import PublicationPipelineService
from app.services.lifecycle.reconciliation import ReconciliationService
from app.workers.celery_app import (
    IngestionJobPayload,
    IngestionTaskDispatcher,
    PipelineTask,
    TaskSender,
    WorkerKind,
    create_celery_app,
    declare_broker_topology,
)

settings = get_settings()
celery = create_celery_app(settings, WorkerKind.PUBLICATION)
celery.conf.beat_schedule = {
    "document-loader-reconciliation": {
        "task": PipelineTask.RECONCILE,
        "schedule": settings.lifecycle.reconcile_interval_seconds,
    }
}


@lru_cache(maxsize=1)
def embedding_provider() -> FastEmbedProvider:
    """Load and verify one immutable model per recycled worker process."""
    return FastEmbedProvider(settings.embedding)


def _run_task(task, payload: dict[str, str], stage: str) -> None:
    validated_payload = IngestionJobPayload.model_validate(payload)
    worker_id = str(task.request.hostname or "publication-worker")[:255]
    try:
        asyncio.run(run_publication_job(validated_payload, stage, worker_id))
    except RetryableIngestionError as error:
        raise task.retry(
            exc=error,
            countdown=error.retry_after_seconds,
            max_retries=settings.workers.job_max_attempts - 1,
        ) from error


@celery.task(bind=True, name=PipelineTask.CHUNK_PROCESS)
def chunk_document(task, payload: dict[str, str]) -> None:
    _run_task(task, payload, "chunk")


@celery.task(bind=True, name=PipelineTask.EMBED_PROCESS)
def embed_document(task, payload: dict[str, str]) -> None:
    _run_task(task, payload, "embed")


@celery.task(bind=True, name=PipelineTask.INDEX_PROCESS)
def index_document(task, payload: dict[str, str]) -> None:
    _run_task(task, payload, "index")


@celery.task(bind=True, name=PipelineTask.RECONCILE)
def reconcile(task) -> int:
    """Run one globally bounded reconciliation page."""
    try:
        return asyncio.run(run_reconciliation())
    except Exception as error:
        raise task.retry(
            exc=error,
            countdown=settings.workers.retry_base_seconds,
            max_retries=settings.workers.job_max_attempts - 1,
        ) from error


async def run_publication_job(
    payload: IngestionJobPayload, stage: str, worker_id: str
) -> None:
    database = Database(settings.database)
    storage = ObjectStorage(
        settings.storage,
        provider_timeouts=settings.provider_timeouts,
    )
    dispatcher = CeleryJobDispatcher(
        IngestionTaskDispatcher(cast(TaskSender, celery), settings.broker)
    )
    service = PublicationPipelineService(
        database,
        storage,
        settings,
        dispatcher,
        embedding_provider=embedding_provider() if stage == "embed" else None,
    )
    try:
        await service.process(payload, stage, worker_id)
    finally:
        await database.close()
        storage.close()


async def run_reconciliation() -> int:
    database = Database(settings.database)
    dispatcher = CeleryJobDispatcher(
        IngestionTaskDispatcher(cast(TaskSender, celery), settings.broker)
    )
    service = ReconciliationService(database, dispatcher, settings)
    try:
        findings = await service.run_once()
        return len(findings)
    finally:
        await database.close()


def main() -> None:
    """Start the publication pool consuming only its configured queue."""
    declare_broker_topology(celery, settings)
    celery.worker_main(
        [
            "worker",
            "--loglevel=INFO",
            "--queues",
            settings.broker.publication_queue_name,
        ]
    )


if __name__ == "__main__":
    main()
