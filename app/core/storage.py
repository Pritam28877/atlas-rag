"""S3-compatible storage primitives for immutable document content."""

import asyncio
import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

import boto3
from botocore.config import Config

from app.core.config import ProviderTimeoutSettings, StorageSettings
from app.core.storage_transfers import (
    StorageTransferIntegrityError,
    download_object_to_path,
    upload_object_from_path,
)


class StorageIntegrityError(ValueError):
    """Raised when stored metadata cannot prove the expected immutable object."""


class ArtifactKind(StrEnum):
    """Derived artifact categories planned for the ingestion pipeline."""

    INSPECTION = "inspection"
    EXTRACTED_PAGES = "extracted-pages"
    LAYOUT_BLOCKS = "layout-blocks"
    OCR_RESULT = "ocr-result"
    NORMALIZED_DOCUMENT = "normalized-document"
    CHUNK_MANIFEST = "chunk-manifest"
    INDEX_MANIFEST = "index-manifest"


class S3Client(Protocol):
    def generate_presigned_url(
        self,
        ClientMethod: str,
        Params: Mapping[str, object],
        ExpiresIn: int,
        HttpMethod: str,
    ) -> str: ...

    def head_object(self, **kwargs: object) -> Mapping[str, object]: ...

    def head_bucket(self, **kwargs: object) -> Mapping[str, object]: ...

    def get_object(self, **kwargs: object) -> Mapping[str, object]: ...

    def put_object(self, **kwargs: object) -> Mapping[str, object]: ...

    def delete_object(self, **kwargs: object) -> Mapping[str, object]: ...

    def close(self) -> None: ...


class ReadableBody(Protocol):
    def read(self, amount: int) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class ObjectReference:
    """Server-generated immutable S3 identity and provenance metadata."""

    key: str
    checksum_sha256: str
    metadata: Mapping[str, str]


@dataclass(frozen=True)
class PresignedUpload:
    """A direct-upload URL and signed headers the caller must provide."""

    url: str
    headers: Mapping[str, str]
    expires_in_seconds: int


@dataclass(frozen=True)
class StoredObjectMetadata:
    """Verified metadata returned by an S3-compatible object store."""

    key: str
    content_length: int
    checksum_sha256: str
    content_type: str | None
    version_id: str | None
    metadata: Mapping[str, str]


def sha256_hex(payload: bytes) -> str:
    """Return the stable hexadecimal SHA-256 identifier for immutable content."""
    return hashlib.sha256(payload).hexdigest()


def checksum_header_value(checksum_sha256: str) -> str:
    """Convert a hexadecimal SHA-256 digest to the S3 checksum header value."""
    try:
        digest = bytes.fromhex(checksum_sha256)
    except ValueError as error:
        raise StorageIntegrityError(
            "checksum must be a hexadecimal SHA-256 digest"
        ) from error
    if len(digest) != hashlib.sha256().digest_size:
        raise StorageIntegrityError("checksum must be a SHA-256 digest")
    return base64.b64encode(digest).decode("ascii")


class ObjectStorage:
    """Generate safe storage references and direct-transfer authorizations."""

    def __init__(
        self,
        settings: StorageSettings,
        client: S3Client | None = None,
        provider_timeouts: ProviderTimeoutSettings | None = None,
    ) -> None:
        required_values = {
            "storage.endpoint_url": settings.endpoint_url,
            "storage.bucket_name": settings.bucket_name,
            "storage.access_key_id": settings.access_key_id,
            "storage.secret_access_key": settings.secret_access_key,
        }
        missing_fields = [name for name, value in required_values.items() if not value]
        if missing_fields:
            raise ValueError(
                f"storage cannot initialize without: {', '.join(missing_fields)}"
            )

        self._settings = settings
        bucket_name = settings.bucket_name
        access_key_id = settings.access_key_id
        secret_access_key = settings.secret_access_key
        assert bucket_name is not None
        assert access_key_id is not None
        assert secret_access_key is not None
        self._bucket_name = bucket_name
        effective_timeouts = provider_timeouts or ProviderTimeoutSettings()
        self._client: S3Client = client or cast(
            S3Client,
            boto3.client(
                "s3",
                endpoint_url=settings.endpoint_url,
                region_name=settings.region,
                aws_access_key_id=access_key_id.get_secret_value(),
                aws_secret_access_key=secret_access_key.get_secret_value(),
                use_ssl=settings.use_tls,
                config=Config(
                    signature_version="s3v4",
                    connect_timeout=effective_timeouts.connect_seconds,
                    read_timeout=effective_timeouts.storage_seconds,
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            ),
        )

    def original_reference(
        self,
        tenant_id: UUID,
        document_version_id: UUID,
        checksum_sha256: str,
    ) -> ObjectReference:
        """Build a tenant-scoped, content-addressed key for one original PDF."""
        checksum_header_value(checksum_sha256)
        key = (
            f"{self._settings.key_prefix}/tenants/{tenant_id}/versions/"
            f"{document_version_id}/originals/{checksum_sha256}.pdf"
        )
        return ObjectReference(
            key=key,
            checksum_sha256=checksum_sha256,
            metadata={
                "tenant-id": str(tenant_id),
                "document-version-id": str(document_version_id),
                "content-sha256": checksum_sha256,
                "content-kind": "original-pdf",
                "retention-class": "managed",
            },
        )

    def artifact_reference(
        self,
        tenant_id: UUID,
        document_version_id: UUID,
        artifact_kind: ArtifactKind,
        pipeline_version: str,
        checksum_sha256: str,
    ) -> ObjectReference:
        """Build a versioned, content-addressed derived-artifact key."""
        checksum_header_value(checksum_sha256)
        if not pipeline_version or "/" in pipeline_version:
            raise ValueError("pipeline_version must be a non-empty path-safe value")
        key = (
            f"{self._settings.key_prefix}/tenants/{tenant_id}/versions/"
            f"{document_version_id}/artifacts/{artifact_kind}/{pipeline_version}/"
            f"{checksum_sha256}.json"
        )
        return ObjectReference(
            key=key,
            checksum_sha256=checksum_sha256,
            metadata={
                "tenant-id": str(tenant_id),
                "document-version-id": str(document_version_id),
                "artifact-kind": artifact_kind,
                "pipeline-version": pipeline_version,
                "content-sha256": checksum_sha256,
                "retention-class": "managed",
            },
        )

    def presign_upload(
        self,
        reference: ObjectReference,
        content_type: str,
    ) -> PresignedUpload:
        """Authorize direct PUT of one generated object without API byte buffering."""
        checksum = checksum_header_value(reference.checksum_sha256)
        parameters: dict[str, object] = {
            "Bucket": self._bucket_name,
            "Key": reference.key,
            "ContentType": content_type,
            "ChecksumSHA256": checksum,
            "Metadata": dict(reference.metadata),
        }
        headers = {
            "content-type": content_type,
            "x-amz-checksum-sha256": checksum,
        }
        if self._settings.server_side_encryption != "provider-default":
            parameters["ServerSideEncryption"] = (
                self._settings.server_side_encryption
            )
            headers["x-amz-server-side-encryption"] = (
                self._settings.server_side_encryption
            )
        for metadata_key, metadata_value in reference.metadata.items():
            headers[f"x-amz-meta-{metadata_key}"] = metadata_value
        if self._settings.kms_key_id:
            parameters["SSEKMSKeyId"] = self._settings.kms_key_id
            headers["x-amz-server-side-encryption-aws-kms-key-id"] = (
                self._settings.kms_key_id
            )
        url = self._client.generate_presigned_url(
            ClientMethod="put_object",
            Params=parameters,
            ExpiresIn=self._settings.signed_url_ttl_seconds,
            HttpMethod="PUT",
        )
        return PresignedUpload(
            url=url,
            headers=headers,
            expires_in_seconds=self._settings.signed_url_ttl_seconds,
        )

    def presign_download(self, reference: ObjectReference) -> str:
        """Authorize a short-lived direct GET for a server-generated object key."""
        return self._client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": self._bucket_name, "Key": reference.key},
            ExpiresIn=self._settings.signed_url_ttl_seconds,
            HttpMethod="GET",
        )

    async def verify_object(
        self,
        reference: ObjectReference,
        expected_content_length: int,
    ) -> StoredObjectMetadata:
        """Verify identity without reading object content through the API."""
        response = await asyncio.to_thread(
            self._client.head_object,
            Bucket=self._bucket_name,
            Key=reference.key,
            ChecksumMode="ENABLED",
        )
        content_length = response.get("ContentLength")
        checksum = response.get("ChecksumSHA256")
        metadata_value = response.get("Metadata")
        metadata = metadata_value if isinstance(metadata_value, Mapping) else {}
        normalized_metadata = {str(key): str(value) for key, value in metadata.items()}
        if content_length != expected_content_length:
            raise StorageIntegrityError(
                "stored object length does not match expected length"
            )
        if checksum != checksum_header_value(reference.checksum_sha256):
            raise StorageIntegrityError(
                "stored object checksum does not match expected checksum"
            )
        if normalized_metadata != dict(reference.metadata):
            raise StorageIntegrityError(
                "stored object provenance metadata does not match"
            )
        version_id = response.get("VersionId")
        content_type = response.get("ContentType")
        return StoredObjectMetadata(
            key=reference.key,
            content_length=expected_content_length,
            checksum_sha256=reference.checksum_sha256,
            content_type=str(content_type) if content_type is not None else None,
            version_id=str(version_id) if version_id is not None else None,
            metadata=normalized_metadata,
        )

    async def read_prefix(self, reference: ObjectReference, byte_count: int) -> bytes:
        """Read a fixed object prefix for inexpensive format classification."""
        if byte_count < 1 or byte_count > 1024:
            raise ValueError("prefix byte count must be between 1 and 1024")
        response = await asyncio.to_thread(
            self._client.get_object,
            Bucket=self._bucket_name,
            Key=reference.key,
            Range=f"bytes=0-{byte_count - 1}",
        )
        body = cast(ReadableBody | None, response.get("Body"))
        if body is None or not hasattr(body, "read") or not hasattr(body, "close"):
            raise StorageIntegrityError("stored object prefix is unavailable")
        try:
            payload = await asyncio.to_thread(body.read, byte_count)
        finally:
            body.close()
        if not isinstance(payload, bytes) or len(payload) > byte_count:
            raise StorageIntegrityError("stored object prefix is invalid")
        return payload

    async def download_to_path(
        self,
        reference: ObjectReference,
        destination: Path,
        maximum_bytes: int,
    ) -> int:
        """Stream one immutable object to a private worker path with a hard bound."""
        try:
            return await asyncio.to_thread(
                download_object_to_path,
                self._client,
                self._bucket_name,
                reference,
                destination,
                maximum_bytes,
            )
        except StorageTransferIntegrityError as error:
            raise StorageIntegrityError(str(error)) from error

    async def upload_from_path(
        self,
        reference: ObjectReference,
        source: Path,
        content_type: str,
        maximum_bytes: int,
    ) -> StoredObjectMetadata:
        """Publish a bounded immutable worker artifact and verify its metadata."""
        size_bytes = source.stat().st_size
        if size_bytes > maximum_bytes:
            raise StorageIntegrityError("artifact exceeds the configured output limit")
        await asyncio.to_thread(
            upload_object_from_path,
            self._client,
            self._bucket_name,
            self._settings,
            reference,
            source,
            content_type,
            checksum_header_value(reference.checksum_sha256),
        )
        return await self.verify_object(reference, size_bytes)

    async def check_connection(self) -> None:
        """Verify bucket access without listing or transferring object content."""
        await asyncio.to_thread(self._client.head_bucket, Bucket=self._bucket_name)

    async def delete_key(self, key: str) -> None:
        """Delete one catalog-owned key without accepting caller-controlled paths."""
        expected_prefix = f"{self._settings.key_prefix}/tenants/"
        if not key.startswith(expected_prefix) or ".." in key:
            raise StorageIntegrityError("object key is outside the managed prefix")
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self._bucket_name,
            Key=key,
        )

    def close(self) -> None:
        """Release the underlying HTTP client connection pool."""
        self._client.close()
