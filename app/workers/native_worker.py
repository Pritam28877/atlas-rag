"""CLI entrypoint for the isolated native-extraction worker pool."""

import asyncio
from typing import cast

from app.core.config import get_settings
from app.core.database import Database
from app.core.storage import ObjectStorage
from app.services.catalog.service import CeleryJobDispatcher
from app.services.ingestion.errors import RetryableIngestionError
from app.services.ingestion.service import NativeIngestionService
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
celery = create_celery_app(settings, WorkerKind.NATIVE)


@celery.task(bind=True, name=PipelineTask.NATIVE_PROCESS)
def process_document(task, payload: dict[str, str]) -> None:
    """Run one durable native job and retry only recorded transient failures."""
    validated_payload = IngestionJobPayload.model_validate(payload)
    worker_id = str(task.request.hostname or "native-worker")[:255]
    try:
        asyncio.run(run_native_job(validated_payload, worker_id))
    except RetryableIngestionError as error:
        raise task.retry(
            exc=error,
            countdown=error.retry_after_seconds,
            max_retries=settings.workers.job_max_attempts - 1,
        ) from error


async def run_native_job(payload: IngestionJobPayload, worker_id: str) -> None:
    database = Database(settings.database)
    storage = ObjectStorage(
        settings.storage,
        provider_timeouts=settings.provider_timeouts,
    )
    dispatcher = CeleryJobDispatcher(
        IngestionTaskDispatcher(cast(TaskSender, celery), settings.broker)
    )
    service = NativeIngestionService(
        database,
        storage,
        settings,
        dispatcher,
    )
    try:
        await service.process(payload, worker_id)
    finally:
        await database.close()
        storage.close()


def main() -> None:
    """Start the native pool consuming only its configured queue."""
    declare_broker_topology(celery, settings)
    celery.worker_main(
        [
            "worker",
            "--loglevel=INFO",
            "--queues",
            settings.broker.native_queue_name,
        ]
    )


if __name__ == "__main__":
    main()
