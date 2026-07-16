"""CLI entrypoint for the isolated OCR worker pool."""

from app.core.config import get_settings
from app.workers.celery_app import (
    IngestionJobPayload,
    PipelineTask,
    WorkerKind,
    create_celery_app,
)

celery = create_celery_app(get_settings(), WorkerKind.OCR)


@celery.task(name=PipelineTask.OCR_PROCESS)
def process_document(payload: dict[str, str]) -> None:
    """Validate the durable OCR notification; P6 installs OCR processing."""
    IngestionJobPayload.model_validate(payload)
    raise RuntimeError("OCR document processing is not installed")
