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
    PUBLICATION = "publication"
    LIFECYCLE = "lifecycle"


class PipelineTask(StrEnum):
    """Only task names accepted by the platform dispatcher."""

    NATIVE_PROCESS = "app.workers.native.process_document"
    OCR_PROCESS = "app.workers.ocr.process_document"
    CHUNK_PROCESS = "app.workers.publication.chunk_document"
    EMBED_PROCESS = "app.workers.publication.embed_document"
    INDEX_PROCESS = "app.workers.publication.index_document"
    RECONCILE = "app.workers.publication.reconcile"
    DELETE_DOCUMENT = "app.workers.lifecycle.delete_document"


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
        if task is PipelineTask.NATIVE_PROCESS:
            return self._broker_settings.native_queue_name
        if task is PipelineTask.DELETE_DOCUMENT:
            return self._broker_settings.lifecycle_queue_name
        if task is PipelineTask.OCR_PROCESS:
            return self._broker_settings.ocr_queue_name
        return self._broker_settings.publication_queue_name


def create_celery_app(settings: Settings, worker_kind: WorkerKind) -> Celery:
    """Create one Celery application configured for a native or OCR worker pool."""
    if settings.broker.url is None:
        raise ValueError("broker.url is required to initialize Celery")

    worker_timeout = _worker_timeout(settings.workers, worker_kind)
    worker_concurrency = _worker_concurrency(settings.workers, worker_kind)
    selected_queue = _queue_for_worker(settings.broker, worker_kind)
    exchange = Exchange("ingestion", type="topic", durable=True)
    queues = (
        _work_queue(
            settings.broker.native_queue_name,
            WorkerKind.NATIVE,
            exchange,
            settings,
        ),
        _work_queue(
            settings.broker.publication_queue_name,
            WorkerKind.PUBLICATION,
            exchange,
            settings,
        ),
        _work_queue(
            settings.broker.ocr_queue_name,
            WorkerKind.OCR,
            exchange,
            settings,
        ),
        _work_queue(
            settings.broker.lifecycle_queue_name,
            WorkerKind.LIFECYCLE,
            exchange,
            settings,
        ),
    )
    app = Celery(
        f"rag-{worker_kind}-worker",
        broker=settings.broker.url.get_secret_value(),
    )
    app.conf.update(
        accept_content=["json"],
        broker_connection_max_retries=settings.broker.connection_max_retries,
        broker_connection_timeout=settings.broker.connection_timeout_seconds,
        broker_connection_retry=True,
        broker_connection_retry_on_startup=True,
        broker_heartbeat=settings.broker.heartbeat_seconds,
        broker_transport_options={
            "confirm_publish": True,
            "max_retries": settings.broker.connection_max_retries,
        },
        result_backend=None,
        result_serializer="json",
        task_acks_late=True,
        task_acks_on_failure_or_timeout=True,
        task_create_missing_queues=False,
        task_default_exchange="ingestion",
        task_default_exchange_type="topic",
        task_default_delivery_mode="persistent",
        task_default_queue=selected_queue,
        task_ignore_result=True,
        task_publish_retry=True,
        task_publish_retry_policy={
            "max_retries": settings.broker.connection_max_retries,
            "interval_start": 0.2,
            "interval_step": 0.5,
            "interval_max": 2.0,
        },
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
            PipelineTask.CHUNK_PROCESS: {
                "queue": settings.broker.publication_queue_name,
                "routing_key": WorkerKind.PUBLICATION,
            },
            PipelineTask.EMBED_PROCESS: {
                "queue": settings.broker.publication_queue_name,
                "routing_key": WorkerKind.PUBLICATION,
            },
            PipelineTask.INDEX_PROCESS: {
                "queue": settings.broker.publication_queue_name,
                "routing_key": WorkerKind.PUBLICATION,
            },
            PipelineTask.RECONCILE: {
                "queue": settings.broker.publication_queue_name,
                "routing_key": WorkerKind.PUBLICATION,
            },
            PipelineTask.DELETE_DOCUMENT: {
                "queue": settings.broker.lifecycle_queue_name,
                "routing_key": WorkerKind.LIFECYCLE,
            },
        },
        task_serializer="json",
        task_soft_time_limit=_soft_time_limit(worker_timeout),
        task_time_limit=worker_timeout,
        task_queues=queues,
        worker_cancel_long_running_tasks_on_connection_loss=True,
        worker_concurrency=worker_concurrency,
        worker_max_tasks_per_child=settings.workers.max_tasks_per_child,
        worker_prefetch_multiplier=settings.broker.prefetch_multiplier,
        worker_send_task_events=False,
        worker_enable_soft_shutdown_on_idle=True,
        worker_soft_shutdown_timeout=settings.workers.shutdown_grace_seconds,
    )
    if settings.broker.use_tls:
        app.conf.broker_use_ssl = _broker_ssl_options(settings.broker)
    return app


def declare_broker_topology(app: Celery, settings: Settings) -> None:
    """Declare every fixed queue before Celery configures delayed delivery."""
    connection = app.connection_for_write()
    try:
        connection.ensure_connection(
            max_retries=settings.broker.connection_max_retries,
            timeout=settings.broker.connection_timeout_seconds,
        )
        channel = connection.channel()
        try:
            dead_letter_exchange = _dead_letter_exchange(settings.broker)
            dead_letter_exchange.bind(channel).declare()
            _dead_letter_queue(
                settings.broker,
                dead_letter_exchange,
            ).bind(channel).declare()
            for queue in app.conf.task_queues:
                queue.bind(channel).declare()
        finally:
            channel.close()
    finally:
        connection.close()


def check_broker_connection(settings: Settings) -> None:
    """Open and close one broker connection without starting a worker process."""
    app = create_celery_app(settings, WorkerKind.NATIVE)
    try:
        connection = app.connection_for_read()
        try:
            connection.ensure_connection(
                max_retries=1,
                timeout=settings.broker.connection_timeout_seconds,
            )
        finally:
            connection.close()
    finally:
        app.close()


def check_worker_connection(settings: Settings) -> None:
    """Require every isolated worker pool on exactly its configured queue."""
    app = create_celery_app(settings, WorkerKind.NATIVE)
    try:
        inspector = app.control.inspect(timeout=2)
        registered_by_worker = inspector.registered() or {}
        queues_by_worker = inspector.active_queues() or {}
        required_pools = {
            WorkerKind.NATIVE: (
                PipelineTask.NATIVE_PROCESS,
                settings.broker.native_queue_name,
            ),
            WorkerKind.OCR: (
                PipelineTask.OCR_PROCESS,
                settings.broker.ocr_queue_name,
            ),
            WorkerKind.PUBLICATION: (
                PipelineTask.CHUNK_PROCESS,
                settings.broker.publication_queue_name,
            ),
            WorkerKind.LIFECYCLE: (
                PipelineTask.DELETE_DOCUMENT,
                settings.broker.lifecycle_queue_name,
            ),
        }
        ready_pools: set[WorkerKind] = set()
        for worker_name, registered_tasks in registered_by_worker.items():
            task_names = {str(task_name) for task_name in registered_tasks}
            worker_queues = queues_by_worker.get(worker_name, [])
            queue_names = {
                str(queue["name"])
                for queue in worker_queues
                if isinstance(queue, dict) and "name" in queue
            }
            matching_pools = [
                worker_kind
                for worker_kind, (task_name, queue_name) in required_pools.items()
                if task_name in task_names and queue_names == {queue_name}
            ]
            if len(matching_pools) == 1:
                ready_pools.add(matching_pools[0])
        missing_pools = set(required_pools) - ready_pools
        if missing_pools:
            raise RuntimeError(
                "one or more required Celery worker pools are unavailable"
            )
    finally:
        app.close()


def _broker_ssl_options(broker_settings: BrokerSettings) -> dict[str, object]:
    options: dict[str, object] = {"cert_reqs": ssl.CERT_REQUIRED}
    if broker_settings.tls_ca_cert_path:
        options["ca_certs"] = broker_settings.tls_ca_cert_path
    if broker_settings.tls_cert_path:
        options["certfile"] = broker_settings.tls_cert_path
        options["keyfile"] = broker_settings.tls_key_path
    return options


def _work_queue(
    name: str,
    routing_key: WorkerKind,
    exchange: Exchange,
    settings: Settings,
) -> Queue:
    return Queue(
        name,
        exchange=exchange,
        routing_key=routing_key,
        durable=True,
        queue_arguments={
            "x-queue-type": "quorum",
            "x-delivery-limit": settings.workers.job_max_attempts,
            "x-max-length": settings.broker.queue_max_messages,
            "x-max-length-bytes": settings.broker.queue_max_bytes,
            "x-overflow": "reject-publish",
            "x-message-ttl": settings.broker.queue_message_ttl_seconds * 1000,
            "x-dead-letter-exchange": settings.broker.dead_letter_exchange_name,
            "x-dead-letter-routing-key": "dead",
        },
    )


def _dead_letter_exchange(broker_settings: BrokerSettings) -> Exchange:
    return Exchange(
        broker_settings.dead_letter_exchange_name,
        type="direct",
        durable=True,
    )


def _dead_letter_queue(
    broker_settings: BrokerSettings,
    exchange: Exchange,
) -> Queue:
    return Queue(
        broker_settings.dead_letter_queue_name,
        exchange=exchange,
        routing_key="dead",
        durable=True,
        queue_arguments={
            "x-queue-type": "quorum",
            "x-max-length": broker_settings.queue_max_messages,
            "x-max-length-bytes": broker_settings.queue_max_bytes,
            "x-overflow": "drop-head",
        },
    )


def _queue_for_worker(broker_settings: BrokerSettings, worker_kind: WorkerKind) -> str:
    if worker_kind is WorkerKind.NATIVE:
        return broker_settings.native_queue_name
    if worker_kind is WorkerKind.OCR:
        return broker_settings.ocr_queue_name
    if worker_kind is WorkerKind.LIFECYCLE:
        return broker_settings.lifecycle_queue_name
    return broker_settings.publication_queue_name


def _worker_concurrency(
    worker_settings: WorkerSettings, worker_kind: WorkerKind
) -> int:
    if worker_kind is WorkerKind.NATIVE:
        return worker_settings.native_concurrency
    if worker_kind is WorkerKind.OCR:
        return worker_settings.ocr_concurrency
    if worker_kind is WorkerKind.LIFECYCLE:
        return worker_settings.lifecycle_concurrency
    return worker_settings.publication_concurrency


def _worker_timeout(worker_settings: WorkerSettings, worker_kind: WorkerKind) -> int:
    if worker_kind is WorkerKind.NATIVE:
        return worker_settings.native_parse_timeout_seconds
    if worker_kind is WorkerKind.OCR:
        return worker_settings.ocr_timeout_seconds
    if worker_kind is WorkerKind.LIFECYCLE:
        return worker_settings.lifecycle_timeout_seconds
    return worker_settings.publication_timeout_seconds


def _soft_time_limit(hard_time_limit: int) -> int | None:
    """Leave at least one second for task cleanup before the hard worker limit."""
    if hard_time_limit <= 1:
        return None
    return hard_time_limit - 1
