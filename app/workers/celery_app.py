"""Bounded Celery configuration for document-ingestion workers."""

import ssl
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from celery import Celery
from kombu import Exchange, Queue
from pydantic import BaseModel, ConfigDict

from app.core.config import BrokerSettings, Settings, WorkerSettings


class WorkerKind(StrEnum):
    """Independent worker pools with no shared execution capacity."""

    NATIVE = "native"
    OCR = "ocr"


class PipelineTask(StrEnum):
    """Only task names accepted by the platform dispatcher."""

    NATIVE_PROCESS = "app.workers.native.process_document"
    OCR_PROCESS = "app.workers.ocr.process_document"
    DELETE_DOCUMENT = "app.workers.native.delete_document"


class IngestionJobPayload(BaseModel):
    """The bounded, ID-only message body shared with worker processes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    document_version_id: UUID
    job_id: UUID


class TaskSender(Protocol):
    def send_task(
        self,
        name: str,
        args: list[dict[str, str]],
        queue: str,
    ) -> object: ...


class IngestionTaskDispatcher:
    """Publish only schema-validated, bounded ID messages to fixed queues."""

    def __init__(self, sender: TaskSender, broker_settings: BrokerSettings) -> None:
        self._sender = sender
        self._broker_settings = broker_settings

    def dispatch(self, task: PipelineTask, payload: IngestionJobPayload) -> None:
        message = payload.model_dump(mode="json")
        serialized_message = payload.model_dump_json().encode("utf-8")
        if len(serialized_message) > self._broker_settings.message_max_bytes:
            raise ValueError("task payload exceeds the configured broker message limit")
        queue = self._queue_for_task(task)
        self._sender.send_task(task, args=[message], queue=queue)

    def _queue_for_task(self, task: PipelineTask) -> str:
        if task in {PipelineTask.NATIVE_PROCESS, PipelineTask.DELETE_DOCUMENT}:
            return self._broker_settings.native_queue_name
        return self._broker_settings.ocr_queue_name


def create_celery_app(settings: Settings, worker_kind: WorkerKind) -> Celery:
    """Create one Celery application configured for a native or OCR worker pool."""
    if settings.broker.url is None:
        raise ValueError("broker.url is required to initialize Celery")

    worker_timeout = _worker_timeout(settings.workers, worker_kind)
    worker_concurrency = _worker_concurrency(settings.workers, worker_kind)
    selected_queue = _queue_for_worker(settings.broker, worker_kind)
    exchange = Exchange("ingestion", type="direct", durable=True)
    queues = (
        Queue(
            settings.broker.native_queue_name,
            exchange=exchange,
            routing_key=WorkerKind.NATIVE,
            durable=True,
            queue_arguments={
                "x-queue-type": "quorum",
                "x-delivery-limit": settings.workers.job_max_attempts,
            },
        ),
        Queue(
            settings.broker.ocr_queue_name,
            exchange=exchange,
            routing_key=WorkerKind.OCR,
            durable=True,
            queue_arguments={
                "x-queue-type": "quorum",
                "x-delivery-limit": settings.workers.job_max_attempts,
            },
        ),
    )
    app = Celery(
        f"rag-{worker_kind}-worker",
        broker=settings.broker.url.get_secret_value(),
    )
    app.conf.update(
        accept_content=["json"],
        broker_connection_max_retries=settings.broker.connection_max_retries,
        broker_connection_retry=True,
        broker_connection_retry_on_startup=True,
        broker_heartbeat=settings.broker.heartbeat_seconds,
        broker_transport_options={
            "visibility_timeout": settings.broker.visibility_timeout_seconds,
        },
        result_backend=None,
        result_serializer="json",
        task_acks_late=True,
        task_acks_on_failure_or_timeout=True,
        task_create_missing_queues=False,
        task_default_exchange="ingestion",
        task_default_exchange_type="direct",
        task_default_queue=selected_queue,
        task_ignore_result=True,
        task_reject_on_worker_lost=True,
        task_routes={
            PipelineTask.NATIVE_PROCESS: {
                "queue": settings.broker.native_queue_name,
                "routing_key": WorkerKind.NATIVE,
            },
            PipelineTask.OCR_PROCESS: {
                "queue": settings.broker.ocr_queue_name,
                "routing_key": WorkerKind.OCR,
            },
            PipelineTask.DELETE_DOCUMENT: {
                "queue": settings.broker.native_queue_name,
                "routing_key": WorkerKind.NATIVE,
            },
        },
        task_serializer="json",
        task_soft_time_limit=_soft_time_limit(worker_timeout),
        task_time_limit=worker_timeout,
        task_queues=queues,
        worker_cancel_long_running_tasks_on_connection_loss=True,
        worker_concurrency=worker_concurrency,
        worker_prefetch_multiplier=settings.broker.prefetch_multiplier,
        worker_send_task_events=False,
        worker_soft_shutdown_timeout=settings.workers.shutdown_grace_seconds,
    )
    if settings.broker.use_tls:
        app.conf.broker_use_ssl = _broker_ssl_options(settings.broker)
    return app


def check_broker_connection(settings: Settings) -> None:
    """Open and close one broker connection without starting a worker process."""
    app = create_celery_app(settings, WorkerKind.NATIVE)
    connection = app.connection_for_read()
    try:
        connection.ensure_connection(
            max_retries=1,
            timeout=settings.broker.connection_timeout_seconds,
        )
    finally:
        connection.close()


def check_worker_connection(settings: Settings) -> None:
    """Require at least one responsive Celery worker without exposing node names."""
    app = create_celery_app(settings, WorkerKind.NATIVE)
    responses = app.control.inspect(timeout=1).ping() or {}
    if not responses:
        raise RuntimeError("no Celery worker responded to broker control ping")


def _broker_ssl_options(broker_settings: BrokerSettings) -> dict[str, object]:
    options: dict[str, object] = {"cert_reqs": ssl.CERT_REQUIRED}
    if broker_settings.tls_ca_cert_path:
        options["ca_certs"] = broker_settings.tls_ca_cert_path
    if broker_settings.tls_cert_path:
        options["certfile"] = broker_settings.tls_cert_path
        options["keyfile"] = broker_settings.tls_key_path
    return options


def _queue_for_worker(broker_settings: BrokerSettings, worker_kind: WorkerKind) -> str:
    if worker_kind is WorkerKind.NATIVE:
        return broker_settings.native_queue_name
    return broker_settings.ocr_queue_name


def _worker_concurrency(
    worker_settings: WorkerSettings, worker_kind: WorkerKind
) -> int:
    if worker_kind is WorkerKind.NATIVE:
        return worker_settings.native_concurrency
    return worker_settings.ocr_concurrency


def _worker_timeout(worker_settings: WorkerSettings, worker_kind: WorkerKind) -> int:
    if worker_kind is WorkerKind.NATIVE:
        return worker_settings.native_parse_timeout_seconds
    return worker_settings.ocr_timeout_seconds


def _soft_time_limit(hard_time_limit: int) -> int | None:
    """Leave at least one second for task cleanup before the hard worker limit."""
    if hard_time_limit <= 1:
        return None
    return hard_time_limit - 1
