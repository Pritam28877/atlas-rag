"""CLI entrypoint for search-aware lifecycle deletion workers."""

import asyncio

from app.core.config import get_settings
from app.core.database import Database
from app.core.storage import ObjectStorage
from app.services.ingestion.errors import RetryableIngestionError
from app.services.lifecycle.deletion_service import DeletionService
from app.workers.celery_app import (
    IngestionJobPayload,
    PipelineTask,
    WorkerKind,
    create_celery_app,
    declare_broker_topology,
)

settings = get_settings()
celery = create_celery_app(settings, WorkerKind.LIFECYCLE)


@celery.task(bind=True, name=PipelineTask.DELETE_DOCUMENT)
def delete_document(task, payload: dict[str, str]) -> None:
    validated_payload = IngestionJobPayload.model_validate(payload)
    worker_id = str(task.request.hostname or "lifecycle-worker")[:255]
    try:
        asyncio.run(run_deletion_job(validated_payload, worker_id))
    except RetryableIngestionError as error:
        raise task.retry(
            exc=error,
            countdown=error.retry_after_seconds,
            max_retries=settings.workers.job_max_attempts - 1,
        ) from error


async def run_deletion_job(payload: IngestionJobPayload, worker_id: str) -> None:
    database = Database(settings.database)
    storage = ObjectStorage(
        settings.storage,
        provider_timeouts=settings.provider_timeouts,
    )
    service = DeletionService(database, storage, settings)
    try:
        await service.process(payload, worker_id)
    finally:
        await database.close()
        storage.close()


def main() -> None:
    """Start the lifecycle pool consuming only its configured queue."""
    declare_broker_topology(celery, settings)
    celery.worker_main(
        [
            "worker",
            "--loglevel=INFO",
            "--queues",
            settings.broker.lifecycle_queue_name,
        ]
    )


if __name__ == "__main__":
    main()
