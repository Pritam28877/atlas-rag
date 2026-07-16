"""CLI entrypoint for the isolated native-extraction worker pool."""

from app.core.config import get_settings
from app.workers.celery_app import (
    IngestionJobPayload,
    PipelineTask,
    WorkerKind,
    create_celery_app,
)

celery = create_celery_app(get_settings(), WorkerKind.NATIVE)


@celery.task(name=PipelineTask.NATIVE_PROCESS)
def process_document(payload: dict[str, str]) -> None:
    """Validate the durable P4 notification; P5 installs native processing."""
    IngestionJobPayload.model_validate(payload)
    raise RuntimeError("native document processing is not installed")


@celery.task(name=PipelineTask.DELETE_DOCUMENT)
def delete_document(payload: dict[str, str]) -> None:
    """Validate the durable deletion notification; P8 installs deletion."""
    IngestionJobPayload.model_validate(payload)
    raise RuntimeError("document deletion processing is not installed")
