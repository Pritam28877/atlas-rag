import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

from app.core.config import StorageSettings


class StorageTransferIntegrityError(ValueError):
    """Raised when a streamed transfer violates identity or size bounds."""


class TransferReference(Protocol):
    @property
    def key(self) -> str: ...

    @property
    def checksum_sha256(self) -> str: ...

    @property
    def metadata(self) -> Mapping[str, str]: ...


class TransferBody(Protocol):
    def read(self, amount: int) -> bytes: ...

    def close(self) -> None: ...


class TransferClient(Protocol):
    def get_object(self, **kwargs: object) -> Mapping[str, object]: ...

    def put_object(self, **kwargs: object) -> Mapping[str, object]: ...


def download_object_to_path(
    client: TransferClient,
    bucket_name: str,
    reference: TransferReference,
    destination: Path,
    maximum_bytes: int,
) -> int:
    if maximum_bytes < 1:
        raise ValueError("maximum_bytes must be positive")
    response = client.get_object(Bucket=bucket_name, Key=reference.key)
    body = cast(TransferBody | None, response.get("Body"))
    if body is None or not hasattr(body, "read") or not hasattr(body, "close"):
        raise StorageTransferIntegrityError("stored object body is unavailable")
    digest = hashlib.sha256()
    total_bytes = 0
    try:
        with destination.open("xb") as output:
            while True:
                chunk = body.read(min(1024 * 1024, maximum_bytes + 1))
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > maximum_bytes:
                    raise StorageTransferIntegrityError(
                        "stored object exceeds the worker download limit"
                    )
                digest.update(chunk)
                output.write(chunk)
    finally:
        body.close()
    if digest.hexdigest() != reference.checksum_sha256:
        raise StorageTransferIntegrityError("downloaded object checksum does not match")
    return total_bytes


def upload_object_from_path(
    client: TransferClient,
    bucket_name: str,
    settings: StorageSettings,
    reference: TransferReference,
    source: Path,
    content_type: str,
    checksum_header: str,
) -> None:
    parameters: dict[str, object] = {
        "Bucket": bucket_name,
        "Key": reference.key,
        "ContentType": content_type,
        "ChecksumSHA256": checksum_header,
        "Metadata": dict(reference.metadata),
    }
    if settings.server_side_encryption != "provider-default":
        parameters["ServerSideEncryption"] = settings.server_side_encryption
    if settings.kms_key_id:
        parameters["SSEKMSKeyId"] = settings.kms_key_id
    with source.open("rb") as body:
        parameters["Body"] = body
        client.put_object(**parameters)
