"""Opaque checksum-protected cursors for configured provider catalogs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from enum import StrEnum
from typing import Self

from pydantic import Field, ValidationError, model_validator

from app.services.harness.protocol import (
    ProviderName,
    Sha256,
    StrictProtocolModel,
)

CURSOR_CHECKSUM_BYTES = 16
MAXIMUM_CATALOG_CURSOR_OFFSET = 1_024


class CatalogCursorKind(StrEnum):
    MODELS = "models"
    PROVIDERS = "providers"


class CatalogCursorError(ValueError):
    pass


class CatalogCursorPayload(StrictProtocolModel):
    version: int = Field(ge=1, le=1)
    kind: CatalogCursorKind
    offset: int = Field(ge=1, le=MAXIMUM_CATALOG_CURSOR_OFFSET)
    snapshot_sha256: Sha256
    provider: ProviderName | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        model_cursor = self.kind is CatalogCursorKind.MODELS
        if model_cursor != (self.provider is not None):
            raise ValueError("model cursor requires provider scope")
        return self


def encode_catalog_cursor(
    *,
    kind: CatalogCursorKind,
    offset: int,
    snapshot_sha256: str,
    provider: str | None = None,
) -> str:
    payload = CatalogCursorPayload(
        version=1,
        kind=kind,
        offset=offset,
        snapshot_sha256=snapshot_sha256,
        provider=provider,
    )
    payload_bytes = payload.model_dump_json().encode()
    checksum = hashlib.sha256(payload_bytes).digest()[
        :CURSOR_CHECKSUM_BYTES
    ]
    encoded = base64.urlsafe_b64encode(checksum + payload_bytes)
    return encoded.rstrip(b"=").decode()


def decode_catalog_cursor(cursor: str) -> CatalogCursorPayload:
    padding = "=" * (-len(cursor) % 4)
    try:
        decoded = base64.b64decode(
            f"{cursor}{padding}",
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as error:
        raise CatalogCursorError("catalog cursor encoding is invalid") from error
    if len(decoded) <= CURSOR_CHECKSUM_BYTES:
        raise CatalogCursorError("catalog cursor is truncated")
    checksum = decoded[:CURSOR_CHECKSUM_BYTES]
    payload_bytes = decoded[CURSOR_CHECKSUM_BYTES:]
    expected_checksum = hashlib.sha256(payload_bytes).digest()[
        :CURSOR_CHECKSUM_BYTES
    ]
    if not hmac.compare_digest(checksum, expected_checksum):
        raise CatalogCursorError("catalog cursor checksum is invalid")
    try:
        return CatalogCursorPayload.model_validate_json(payload_bytes)
    except ValidationError as error:
        raise CatalogCursorError("catalog cursor payload is invalid") from error
