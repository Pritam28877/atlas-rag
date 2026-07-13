"""S3-compatible storage primitives for immutable document content."""

import asyncio
import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

import boto3
from botocore.config import Config

from app.core.config import ProviderTimeoutSettings, StorageSettings


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
        self._bucket_name = settings.bucket_name
        effective_timeouts = provider_timeouts or ProviderTimeoutSettings()
        self._client = client or boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            region_name=settings.region,
            aws_access_key_id=settings.access_key_id.get_secret_value(),
            aws_secret_access_key=settings.secret_access_key.get_secret_value(),
            use_ssl=settings.use_tls,
            config=Config(
                signature_version="s3v4",
                connect_timeout=effective_timeouts.connect_seconds,
                read_timeout=effective_timeouts.storage_seconds,
                retries={"max_attempts": 3, "mode": "standard"},
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
            "ServerSideEncryption": self._settings.server_side_encryption,
        }
        headers = {
            "content-type": content_type,
            "x-amz-checksum-sha256": checksum,
            "x-amz-server-side-encryption": self._settings.server_side_encryption,
        }
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
        return StoredObjectMetadata(
            key=reference.key,
            content_length=expected_content_length,
            checksum_sha256=reference.checksum_sha256,
            version_id=str(version_id) if version_id is not None else None,
            metadata=normalized_metadata,
        )

    async def check_connection(self) -> None:
        """Verify bucket access without listing or transferring object content."""
        await asyncio.to_thread(self._client.head_bucket, Bucket=self._bucket_name)

    def close(self) -> None:
        """Release the underlying HTTP client connection pool."""
        self._client.close()
