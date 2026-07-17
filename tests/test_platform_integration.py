"""Opt-in proof of direct S3 transfer and RabbitMQ broker readiness."""

import asyncio
import os
import time
from contextlib import suppress
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import pytest
from kombu import Exchange, Producer, Queue
from pydantic import SecretStr

from app.core.config import (
    BrokerSettings,
    ProviderTimeoutSettings,
    Settings,
    StorageSettings,
)
from app.core.storage import ObjectStorage, sha256_hex
from app.workers.celery_app import (
    WorkerKind,
    check_broker_connection,
    create_celery_app,
    declare_broker_topology,
)

TENANT_ID = UUID("a7006ca9-bac4-4702-acaf-1e7c0dd6e7b6")
VERSION_ID = UUID("1dd21a4f-c0dc-45b4-8e36-5b9c9a7012a9")


def required_environment_value(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        pytest.skip(f"{name} is not configured")
    return value


@pytest.mark.platform_integration
def test_minio_presigned_transfer_and_metadata_verification() -> None:
    storage = ObjectStorage(
        StorageSettings(
            endpoint_url=required_environment_value("P2_STORAGE_ENDPOINT_URL"),
            bucket_name=required_environment_value("P2_STORAGE_BUCKET_NAME"),
            access_key_id=SecretStr(
                required_environment_value("P2_STORAGE_ACCESS_KEY_ID")
            ),
            secret_access_key=SecretStr(
                required_environment_value("P2_STORAGE_SECRET_ACCESS_KEY")
            ),
            use_tls=required_environment_value("P2_STORAGE_USE_TLS").lower() == "true",
            server_side_encryption=required_environment_value(
                "P2_STORAGE_SERVER_SIDE_ENCRYPTION"
            ),
        ),
        provider_timeouts=ProviderTimeoutSettings(),
    )
    payload = b"%PDF-1.4\nlocal P2 integration fixture\n"
    reference = storage.original_reference(TENANT_ID, VERSION_ID, sha256_hex(payload))
    upload = storage.presign_upload(reference, "application/pdf")

    try:
        upload_request = Request(
            upload.url,
            data=payload,
            headers=dict(upload.headers),
            method="PUT",
        )
        with urlopen(upload_request, timeout=10) as response:
            assert response.status in {200, 204}

        verified = asyncio.run(
            storage.verify_object(reference, expected_content_length=len(payload))
        )
        assert verified.checksum_sha256 == reference.checksum_sha256

        with urlopen(storage.presign_download(reference), timeout=10) as response:
            assert response.read() == payload
    finally:
        storage._client.delete_object(Bucket=storage._bucket_name, Key=reference.key)
        storage.close()


@pytest.mark.platform_integration
def test_rabbitmq_broker_connection() -> None:
    settings = Settings(
        broker=BrokerSettings(
            url=SecretStr(required_environment_value("P2_BROKER_URL")),
            use_tls=required_environment_value("P2_BROKER_USE_TLS").lower() == "true",
        )
    )

    check_broker_connection(settings)


@pytest.mark.platform_integration
def test_rabbitmq_topology_is_idempotent_and_routes_expired_work_to_dlq() -> None:
    prefix = f"rag-integration-{uuid4().hex}"
    broker = BrokerSettings(
        url=SecretStr(required_environment_value("P2_BROKER_URL")),
        use_tls=required_environment_value("P2_BROKER_USE_TLS").lower() == "true",
        native_queue_name=f"{prefix}.native",
        ocr_queue_name=f"{prefix}.ocr",
        publication_queue_name=f"{prefix}.publication",
        lifecycle_queue_name=f"{prefix}.lifecycle",
        dead_letter_exchange_name=f"{prefix}.dead-letter.exchange",
        dead_letter_queue_name=f"{prefix}.dead-letter",
        queue_max_messages=10,
        queue_max_bytes=1024 * 1024,
    )
    settings = Settings(broker=broker)
    app = create_celery_app(settings, WorkerKind.NATIVE)
    connection = None
    try:
        declare_broker_topology(app, settings)
        declare_broker_topology(app, settings)
        connection = app.connection_for_write()
        connection.ensure_connection(max_retries=1, timeout=5)
        channel = connection.channel()
        try:
            producer = Producer(channel)
            producer.publish(
                {"probe": "expired-work"},
                exchange=Exchange("ingestion", type="topic", durable=True),
                routing_key=WorkerKind.NATIVE.value,
                serializer="json",
                delivery_mode=2,
                expiration=0.1,
                retry=False,
            )
            dead_letter_queue = Queue(broker.dead_letter_queue_name).bind(channel)
            deadline = time.monotonic() + 10
            dead_letter_message = None
            while time.monotonic() < deadline and dead_letter_message is None:
                dead_letter_message = dead_letter_queue.get(no_ack=True)
                if dead_letter_message is None:
                    time.sleep(0.1)
            assert dead_letter_message is not None
            assert dead_letter_message.payload == {"probe": "expired-work"}
        finally:
            channel.close()

        mismatched_settings = Settings(
            broker=broker.model_copy(update={"queue_max_messages": 11})
        )
        mismatched_app = create_celery_app(
            mismatched_settings,
            WorkerKind.NATIVE,
        )
        try:
            with pytest.raises(Exception, match="PRECONDITION_FAILED|inequivalent arg"):
                declare_broker_topology(mismatched_app, mismatched_settings)
        finally:
            mismatched_app.close()
    finally:
        if connection is not None:
            connection.close()
        _delete_broker_test_topology(app, broker)
        app.close()


def _delete_broker_test_topology(app, broker: BrokerSettings) -> None:
    with suppress(Exception):
        connection = app.connection_for_write()
        try:
            channel = connection.channel()
            try:
                for queue_name in (
                    broker.native_queue_name,
                    broker.ocr_queue_name,
                    broker.publication_queue_name,
                    broker.lifecycle_queue_name,
                    broker.dead_letter_queue_name,
                ):
                    channel.queue_delete(queue=queue_name)
                channel.exchange_delete(exchange=broker.dead_letter_exchange_name)
            finally:
                channel.close()
        finally:
            connection.close()
