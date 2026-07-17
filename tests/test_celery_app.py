import importlib
import sys
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import (
    BrokerSettings,
    DatabaseSettings,
    Settings,
    StorageSettings,
)
from app.workers import celery_app as celery_app_module
from app.workers.celery_app import (
    IngestionJobPayload,
    IngestionTaskDispatcher,
    PipelineTask,
    WorkerKind,
    create_celery_app,
    declare_broker_topology,
)

TENANT_ID = UUID("a7006ca9-bac4-4702-acaf-1e7c0dd6e7b6")
VERSION_ID = UUID("1dd21a4f-c0dc-45b4-8e36-5b9c9a7012a9")
JOB_ID = UUID("e4f1e42d-cc5f-48d7-b476-46743e94b5dc")


class RecordingSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, str]], str]] = []

    def send_task(
        self,
        name: str,
        args: list[dict[str, str]],
        queue: str,
    ) -> None:
        self.calls.append((name, args, queue))


class RecordingChannel:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class RecordingConnection:
    def __init__(self) -> None:
        self.channel_instance = RecordingChannel()
        self.ensure_calls: list[tuple[int, float]] = []
        self.closed = False

    def ensure_connection(self, *, max_retries: int, timeout: float) -> None:
        self.ensure_calls.append((max_retries, timeout))

    def channel(self) -> RecordingChannel:
        return self.channel_instance

    def close(self) -> None:
        self.closed = True


def broker_settings() -> BrokerSettings:
    return BrokerSettings(url=SecretStr("amqps://user:password@broker.test/rag"))


def test_celery_native_worker_uses_durable_bounded_configuration() -> None:
    settings = Settings(
        broker=broker_settings(),
        database=DatabaseSettings(
            url=SecretStr("postgresql://user:password@db.test/rag")
        ),
        storage=StorageSettings(
            endpoint_url="https://storage.test",
            bucket_name="rag-documents",
            access_key_id=SecretStr("access-key"),
            secret_access_key=SecretStr("secret-key"),
        ),
    )

    app = create_celery_app(settings, WorkerKind.NATIVE)

    assert app.conf.task_serializer == "json"
    assert app.conf.accept_content == ["json"]
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.worker_concurrency == 2
    assert app.conf.task_time_limit == 300
    assert app.conf.task_soft_time_limit == 299
    assert app.conf.task_default_queue == "ingestion.native"
    assert app.conf.task_default_exchange_type == "topic"
    assert app.conf.broker_use_ssl["cert_reqs"] == 2
    assert {queue.name for queue in app.conf.task_queues} == {
        "ingestion.native",
        "ingestion.ocr",
        "ingestion.publication",
        "ingestion.lifecycle",
    }
    for queue in app.conf.task_queues:
        assert queue.queue_arguments == {
            "x-queue-type": "quorum",
            "x-delivery-limit": 3,
            "x-max-length": 10_000,
            "x-max-length-bytes": 256 * 1024 * 1024,
            "x-overflow": "reject-publish",
            "x-message-ttl": 86_400_000,
            "x-dead-letter-exchange": "ingestion.dead-letter.exchange",
            "x-dead-letter-routing-key": "dead",
        }
    assert app.conf.broker_transport_options["confirm_publish"] is True
    assert app.conf.broker_connection_timeout == 5.0
    assert app.conf.task_default_delivery_mode == "persistent"
    assert app.conf.task_publish_retry is True


def test_celery_ocr_worker_has_independent_limits() -> None:
    settings = Settings(broker=broker_settings())

    app = create_celery_app(settings, WorkerKind.OCR)

    assert app.conf.worker_concurrency == 1
    assert app.conf.worker_max_tasks_per_child == 25
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_time_limit == 900
    assert app.conf.task_default_queue == "ingestion.ocr"


def test_celery_publication_worker_has_independent_limits() -> None:
    settings = Settings(broker=broker_settings())

    app = create_celery_app(settings, WorkerKind.PUBLICATION)

    assert app.conf.worker_concurrency == 1
    assert app.conf.worker_max_tasks_per_child == 25
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_time_limit == 900
    assert app.conf.task_default_queue == "ingestion.publication"


def test_celery_lifecycle_worker_has_independent_limits() -> None:
    settings = Settings(
        broker=broker_settings(),
        workers={"lifecycle_concurrency": 3, "lifecycle_timeout_seconds": 240},
    )

    app = create_celery_app(settings, WorkerKind.LIFECYCLE)

    assert app.conf.worker_concurrency == 3
    assert app.conf.task_time_limit == 240
    assert app.conf.task_soft_time_limit == 239
    assert app.conf.task_default_queue == "ingestion.lifecycle"


def test_broker_topology_declares_all_queues_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(broker=broker_settings())
    app = create_celery_app(settings, WorkerKind.NATIVE)
    connection = RecordingConnection()
    declared_queues: list[str] = []

    monkeypatch.setattr(app, "connection_for_write", lambda: connection)
    def declarable(name: str):
        return type(
            "Declarable",
            (),
            {
                "bind": lambda self, channel: type(
                    "BoundDeclarable",
                    (),
                    {"declare": lambda self: declared_queues.append(name)},
                )(),
            },
        )()

    monkeypatch.setattr(
        celery_app_module,
        "_dead_letter_exchange",
        lambda broker: declarable(broker.dead_letter_exchange_name),
    )
    monkeypatch.setattr(
        celery_app_module,
        "_dead_letter_queue",
        lambda broker, exchange: declarable(broker.dead_letter_queue_name),
    )
    for queue in app.conf.task_queues:
        monkeypatch.setattr(
            queue,
            "bind",
            lambda channel, queue=queue: type(
                "BoundQueue",
                (),
                {"declare": lambda self: declared_queues.append(queue.name)},
            )(),
        )

    declare_broker_topology(app, settings)

    assert connection.ensure_calls == [(5, 5.0)]
    assert declared_queues == [
        "ingestion.dead-letter.exchange",
        "ingestion.dead-letter",
        "ingestion.native",
        "ingestion.publication",
        "ingestion.ocr",
        "ingestion.lifecycle",
    ]
    assert connection.channel_instance.closed is True
    assert connection.closed is True


def test_dispatcher_publishes_only_id_payload_to_fixed_queue() -> None:
    sender = RecordingSender()
    dispatcher = IngestionTaskDispatcher(sender, broker_settings())
    payload = IngestionJobPayload(
        tenant_id=TENANT_ID,
        document_version_id=VERSION_ID,
        job_id=JOB_ID,
    )

    dispatcher.dispatch(PipelineTask.NATIVE_PROCESS, payload)

    assert sender.calls == [
        (
            PipelineTask.NATIVE_PROCESS,
            [
                {
                    "tenant_id": str(TENANT_ID),
                    "document_version_id": str(VERSION_ID),
                    "job_id": str(JOB_ID),
                }
            ],
            "ingestion.native",
        )
    ]


def test_dispatcher_routes_deletion_to_lifecycle_queue() -> None:
    sender = RecordingSender()
    dispatcher = IngestionTaskDispatcher(sender, broker_settings())
    payload = IngestionJobPayload(
        tenant_id=TENANT_ID,
        document_version_id=VERSION_ID,
        job_id=JOB_ID,
    )

    dispatcher.dispatch(PipelineTask.DELETE_DOCUMENT, payload)

    assert sender.calls[0][0] == PipelineTask.DELETE_DOCUMENT
    assert sender.calls[0][2] == "ingestion.lifecycle"


def test_dispatcher_routes_publication_stages_to_publication_queue() -> None:
    sender = RecordingSender()
    dispatcher = IngestionTaskDispatcher(sender, broker_settings())
    payload = IngestionJobPayload(
        tenant_id=TENANT_ID,
        document_version_id=VERSION_ID,
        job_id=JOB_ID,
    )

    for task in (
        PipelineTask.CHUNK_PROCESS,
        PipelineTask.EMBED_PROCESS,
        PipelineTask.INDEX_PROCESS,
    ):
        dispatcher.dispatch(task, payload)

    assert [call[2] for call in sender.calls] == ["ingestion.publication"] * 3


def test_native_worker_registers_dispatched_task_names(monkeypatch) -> None:
    monkeypatch.setenv("BROKER__URL", "amqp://user:password@broker.test/rag")
    monkeypatch.setenv("BROKER__USE_TLS", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    sys.modules.pop("app.workers.native_worker", None)
    native_worker = importlib.import_module("app.workers.native_worker")

    assert PipelineTask.NATIVE_PROCESS in native_worker.celery.tasks
    assert PipelineTask.DELETE_DOCUMENT not in native_worker.celery.tasks
    get_settings.cache_clear()


def test_lifecycle_worker_registers_deletion_task(monkeypatch) -> None:
    monkeypatch.setenv("BROKER__URL", "amqp://user:password@broker.test/rag")
    monkeypatch.setenv("BROKER__USE_TLS", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    sys.modules.pop("app.workers.lifecycle_worker", None)
    lifecycle_worker = importlib.import_module("app.workers.lifecycle_worker")

    assert PipelineTask.DELETE_DOCUMENT in lifecycle_worker.celery.tasks
    get_settings.cache_clear()


def test_publication_worker_registers_all_stage_tasks(monkeypatch) -> None:
    monkeypatch.setenv("BROKER__URL", "amqp://user:password@broker.test/rag")
    monkeypatch.setenv("BROKER__USE_TLS", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    sys.modules.pop("app.workers.publication_worker", None)
    publication_worker = importlib.import_module("app.workers.publication_worker")

    for task in (
        PipelineTask.CHUNK_PROCESS,
        PipelineTask.EMBED_PROCESS,
        PipelineTask.INDEX_PROCESS,
        PipelineTask.RECONCILE,
    ):
        assert task in publication_worker.celery.tasks
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("module_name", "queue_name"),
    [
        ("app.workers.native_worker", "ingestion.native"),
        ("app.workers.ocr_worker", "ingestion.ocr"),
        ("app.workers.publication_worker", "ingestion.publication"),
        ("app.workers.lifecycle_worker", "ingestion.lifecycle"),
    ],
)
def test_worker_main_consumes_only_its_configured_queue(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    queue_name: str,
) -> None:
    monkeypatch.setenv("BROKER__URL", "amqp://user:password@broker.test/rag")
    monkeypatch.setenv("BROKER__USE_TLS", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    sys.modules.pop(module_name, None)
    worker_module = importlib.import_module(module_name)
    calls: list[list[str]] = []
    declared_apps: list[object] = []
    monkeypatch.setattr(
        worker_module,
        "declare_broker_topology",
        lambda app, settings: declared_apps.append(app),
    )
    monkeypatch.setattr(worker_module.celery, "worker_main", calls.append)

    worker_module.main()

    assert calls == [
        ["worker", "--loglevel=INFO", "--queues", queue_name]
    ]
    assert declared_apps == [worker_module.celery]
    get_settings.cache_clear()


def test_payload_rejects_document_bytes_urls_and_extra_metadata() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IngestionJobPayload.model_validate(
            {
                "tenant_id": TENANT_ID,
                "document_version_id": VERSION_ID,
                "job_id": JOB_ID,
                "signed_url": "https://unsafe.example.test/document.pdf",
            }
        )


def test_broker_url_scheme_must_match_tls_mode() -> None:
    with pytest.raises(ValidationError, match="must match use_tls"):
        BrokerSettings(
            url=SecretStr("amqp://user:password@broker.test/rag"),
            use_tls=True,
        )
