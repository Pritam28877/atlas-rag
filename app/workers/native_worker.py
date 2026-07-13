"""CLI entrypoint for the isolated native-extraction worker pool."""

from app.core.config import get_settings
from app.workers.celery_app import WorkerKind, create_celery_app

celery = create_celery_app(get_settings(), WorkerKind.NATIVE)
