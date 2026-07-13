"""Opt-in proof of direct S3 transfer and RabbitMQ broker readiness."""

import asyncio
import os
from urllib.request import Request, urlopen
from uuid import UUID

import pytest
from pydantic import SecretStr

from app.core.config import (
    BrokerSettings,
    ProviderTimeoutSettings,
    Settings,
    StorageSettings,
)
from app.core.storage import ObjectStorage, sha256_hex
from app.workers.celery_app import check_broker_connection

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
