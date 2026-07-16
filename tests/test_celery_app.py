import importlib
import sys
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import BrokerSettings, Settings
from app.workers.celery_app import (
    IngestionJobPayload,
    IngestionTaskDispatcher,
    PipelineTask,
    WorkerKind,
    create_celery_app,
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


def broker_settings() -> BrokerSettings:
    return BrokerSettings(url=SecretStr("amqps://user:password@broker.test/rag"))


def test_celery_native_worker_uses_durable_bounded_configuration() -> None:
    settings = Settings(
        broker=broker_settings(),
        database={"url": "postgresql://user:password@db.test/rag"},
        storage={
            "endpoint_url": "https://storage.test",
            "bucket_name": "rag-documents",
            "access_key_id": "access-key",
            "secret_access_key": "secret-key",
        },
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
    assert app.conf.broker_use_ssl["cert_reqs"] == 2
    assert {queue.name for queue in app.conf.task_queues} == {
        "ingestion.native",
        "ingestion.ocr",
    }


def test_celery_ocr_worker_has_independent_limits() -> None:
    settings = Settings(broker=broker_settings())

    app = create_celery_app(settings, WorkerKind.OCR)

    assert app.conf.worker_concurrency == 1
    assert app.conf.task_time_limit == 900
    assert app.conf.task_default_queue == "ingestion.ocr"


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


def test_dispatcher_routes_deletion_to_native_queue() -> None:
    sender = RecordingSender()
    dispatcher = IngestionTaskDispatcher(sender, broker_settings())
    payload = IngestionJobPayload(
        tenant_id=TENANT_ID,
        document_version_id=VERSION_ID,
        job_id=JOB_ID,
    )

    dispatcher.dispatch(PipelineTask.DELETE_DOCUMENT, payload)

    assert sender.calls[0][0] == PipelineTask.DELETE_DOCUMENT
    assert sender.calls[0][2] == "ingestion.native"


def test_native_worker_registers_dispatched_task_names(monkeypatch) -> None:
    monkeypatch.setenv("BROKER__URL", "amqp://user:password@broker.test/rag")
    monkeypatch.setenv("BROKER__USE_TLS", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    sys.modules.pop("app.workers.native_worker", None)
    native_worker = importlib.import_module("app.workers.native_worker")

    assert PipelineTask.NATIVE_PROCESS in native_worker.celery.tasks
    assert PipelineTask.DELETE_DOCUMENT in native_worker.celery.tasks
    get_settings.cache_clear()


def test_payload_rejects_document_bytes_urls_and_extra_metadata() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IngestionJobPayload(
            tenant_id=TENANT_ID,
            document_version_id=VERSION_ID,
            job_id=JOB_ID,
            signed_url="https://unsafe.example.test/document.pdf",
        )


def test_broker_url_scheme_must_match_tls_mode() -> None:
    with pytest.raises(ValidationError, match="must match use_tls"):
        BrokerSettings(
            url=SecretStr("amqp://user:password@broker.test/rag"),
            use_tls=True,
        )
