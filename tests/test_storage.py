import asyncio
from collections.abc import Mapping
from io import BytesIO
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import StorageSettings
from app.core.storage import (
    ArtifactKind,
    ObjectStorage,
    StorageIntegrityError,
    checksum_header_value,
    sha256_hex,
)

TENANT_ID = UUID("a7006ca9-bac4-4702-acaf-1e7c0dd6e7b6")
VERSION_ID = UUID("1dd21a4f-c0dc-45b4-8e36-5b9c9a7012a9")


class RecordingS3Client:
    def __init__(self) -> None:
        self.presign_calls: list[tuple[str, Mapping[str, object], int, str]] = []
        self.head_response: Mapping[str, object] = {}
        self.head_calls: list[Mapping[str, object]] = []
        self.get_response: Mapping[str, object] = {}
        self.get_calls: list[Mapping[str, object]] = []
        self.put_calls: list[Mapping[str, object]] = []
        self.closed = False

    def generate_presigned_url(
        self,
        ClientMethod: str,
        Params: Mapping[str, object],
        ExpiresIn: int,
        HttpMethod: str,
    ) -> str:
        self.presign_calls.append((ClientMethod, Params, ExpiresIn, HttpMethod))
        return "https://storage.example.test/presigned"

    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        self.head_calls.append(kwargs)
        return self.head_response

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        self.get_calls.append(kwargs)
        return self.get_response

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        self.put_calls.append(kwargs)
        return {}

    def close(self) -> None:
        self.closed = True


def create_storage() -> tuple[ObjectStorage, RecordingS3Client]:
    settings = StorageSettings(
        endpoint_url="https://storage.example.test",
        bucket_name="rag-documents",
        access_key_id=SecretStr("access-key"),
        secret_access_key=SecretStr("secret-key"),
    )
    client = RecordingS3Client()
    return ObjectStorage(settings, client), client


def test_original_key_is_tenant_scoped_and_content_addressed() -> None:
    storage, _ = create_storage()
    checksum = sha256_hex(b"safe PDF content")

    reference = storage.original_reference(TENANT_ID, VERSION_ID, checksum)

    assert reference.key == (
        f"rag/tenants/{TENANT_ID}/versions/{VERSION_ID}/originals/{checksum}.pdf"
    )
    assert reference.metadata["tenant-id"] == str(TENANT_ID)
    assert "../" not in reference.key


def test_artifact_key_uses_versioned_pipeline_path() -> None:
    storage, _ = create_storage()
    checksum = sha256_hex(b"normalized document")

    reference = storage.artifact_reference(
        TENANT_ID,
        VERSION_ID,
        ArtifactKind.NORMALIZED_DOCUMENT,
        "parser-v1",
        checksum,
    )

    assert "/artifacts/normalized-document/parser-v1/" in reference.key
    assert reference.metadata["pipeline-version"] == "parser-v1"


def test_presigned_upload_signs_checksum_encryption_and_metadata() -> None:
    storage, client = create_storage()
    reference = storage.original_reference(TENANT_ID, VERSION_ID, sha256_hex(b"PDF"))

    upload = storage.presign_upload(reference, "application/pdf")

    method, parameters, expires_in, http_method = client.presign_calls[0]
    assert upload.url == "https://storage.example.test/presigned"
    assert method == "put_object"
    assert http_method == "PUT"
    assert expires_in == 900
    assert parameters["Key"] == reference.key
    assert parameters["Metadata"] == dict(reference.metadata)
    assert upload.headers["x-amz-meta-tenant-id"] == str(TENANT_ID)
    assert upload.headers["x-amz-checksum-sha256"] == checksum_header_value(
        reference.checksum_sha256
    )
    assert upload.headers["x-amz-server-side-encryption"] == "AES256"


def test_provider_default_encryption_omits_per_object_encryption_headers() -> None:
    settings = StorageSettings(
        endpoint_url="https://storage.example.test",
        bucket_name="rag-documents",
        access_key_id=SecretStr("access-key"),
        secret_access_key=SecretStr("secret-key"),
        server_side_encryption="provider-default",
    )
    client = RecordingS3Client()
    storage = ObjectStorage(settings, client)
    reference = storage.original_reference(TENANT_ID, VERSION_ID, sha256_hex(b"PDF"))

    upload = storage.presign_upload(reference, "application/pdf")

    _, parameters, _, _ = client.presign_calls[0]
    assert "ServerSideEncryption" not in parameters
    assert "x-amz-server-side-encryption" not in upload.headers


def test_presigned_download_uses_only_generated_key() -> None:
    storage, client = create_storage()
    reference = storage.original_reference(TENANT_ID, VERSION_ID, sha256_hex(b"PDF"))

    storage.presign_download(reference)

    method, parameters, _, http_method = client.presign_calls[0]
    assert method == "get_object"
    assert http_method == "GET"
    assert parameters == {"Bucket": "rag-documents", "Key": reference.key}


def test_boto3_generates_a_direct_upload_url_without_network_io() -> None:
    settings = StorageSettings(
        endpoint_url="https://storage.example.test",
        bucket_name="rag-documents",
        access_key_id=SecretStr("access-key"),
        secret_access_key=SecretStr("secret-key"),
    )
    storage = ObjectStorage(settings)
    reference = storage.original_reference(TENANT_ID, VERSION_ID, sha256_hex(b"PDF"))

    try:
        upload = storage.presign_upload(reference, "application/pdf")
    finally:
        storage.close()

    assert upload.url.startswith("https://storage.example.test/rag-documents/")
    assert "X-Amz-Signature=" in upload.url


def test_verify_object_checks_length_checksum_and_provenance() -> None:
    storage, client = create_storage()
    reference = storage.original_reference(TENANT_ID, VERSION_ID, sha256_hex(b"PDF"))
    client.head_response = {
        "ContentLength": 3,
        "ChecksumSHA256": checksum_header_value(reference.checksum_sha256),
        "Metadata": dict(reference.metadata),
        "ContentType": "application/pdf",
        "VersionId": "version-1",
    }

    metadata = asyncio.run(storage.verify_object(reference, expected_content_length=3))

    assert metadata.version_id == "version-1"
    assert metadata.content_type == "application/pdf"
    assert client.head_calls == [
        {
            "Bucket": "rag-documents",
            "Key": reference.key,
            "ChecksumMode": "ENABLED",
        }
    ]


def test_read_prefix_uses_bounded_range_and_closes_body() -> None:
    storage, client = create_storage()
    reference = storage.original_reference(TENANT_ID, VERSION_ID, sha256_hex(b"PDF"))
    body = BytesIO(b"%PDF-extra")
    client.get_response = {"Body": body}

    prefix = asyncio.run(storage.read_prefix(reference, 5))

    assert prefix == b"%PDF-"
    assert body.closed
    assert client.get_calls == [
        {
            "Bucket": "rag-documents",
            "Key": reference.key,
            "Range": "bytes=0-4",
        }
    ]


def test_verify_object_rejects_checksum_mismatch() -> None:
    storage, client = create_storage()
    reference = storage.original_reference(TENANT_ID, VERSION_ID, sha256_hex(b"PDF"))
    client.head_response = {
        "ContentLength": 3,
        "ChecksumSHA256": checksum_header_value(sha256_hex(b"wrong")),
        "Metadata": dict(reference.metadata),
    }

    with pytest.raises(StorageIntegrityError, match="checksum"):
        asyncio.run(storage.verify_object(reference, expected_content_length=3))


def test_storage_close_releases_client_resources() -> None:
    storage, client = create_storage()

    storage.close()

    assert client.closed


def test_worker_download_is_streamed_bounded_and_checksum_verified(
    tmp_path,
) -> None:
    storage, client = create_storage()
    payload = b"%PDF-worker-payload"
    reference = storage.original_reference(TENANT_ID, VERSION_ID, sha256_hex(payload))
    body = BytesIO(payload)
    client.get_response = {"Body": body}
    destination = tmp_path / "document.pdf"

    downloaded = asyncio.run(
        storage.download_to_path(reference, destination, maximum_bytes=len(payload))
    )

    assert downloaded == len(payload)
    assert destination.read_bytes() == payload
    assert body.closed


def test_worker_artifact_upload_is_bounded_and_verified(tmp_path) -> None:
    storage, client = create_storage()
    payload = b'{"record_type":"manifest"}\n'
    source = tmp_path / "artifact.jsonl"
    source.write_bytes(payload)
    reference = storage.artifact_reference(
        TENANT_ID,
        VERSION_ID,
        ArtifactKind.NORMALIZED_DOCUMENT,
        "pypdf-6.14.2",
        sha256_hex(payload),
    )
    client.head_response = {
        "ContentLength": len(payload),
        "ChecksumSHA256": checksum_header_value(reference.checksum_sha256),
        "Metadata": dict(reference.metadata),
        "ContentType": "application/json",
    }

    metadata = asyncio.run(
        storage.upload_from_path(
            reference,
            source,
            "application/json",
            maximum_bytes=len(payload),
        )
    )

    assert metadata.content_length == len(payload)
    assert client.put_calls[0]["Key"] == reference.key
    assert client.put_calls[0]["Body"].closed


def test_kms_encryption_requires_key_id() -> None:
    with pytest.raises(ValidationError, match="kms_key_id"):
        StorageSettings(server_side_encryption="aws:kms")
