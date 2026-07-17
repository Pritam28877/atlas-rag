"""Bounded JSONL vector manifests for durable embedding publication."""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.services.ingestion.chunking import ChunkRecord, iter_chunk_manifest
from app.services.ingestion.embedding_provider import EmbeddingProvider
from app.services.ingestion.publication_models import EmbeddingRecord


class EmbeddedChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: UUID
    vector: tuple[float, ...]
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def write_embedding_manifest(
    chunk_manifest_path: Path,
    destination_path: Path,
    tenant_id: UUID,
    document_version_id: UUID,
    provider: EmbeddingProvider,
    batch_size: int,
) -> int:
    count = 0
    pending: list[ChunkRecord] = []
    with destination_path.open("x", encoding="utf-8") as output:
        _write_line(
            output,
            {
                "record_type": "header",
                "schema_version": "embedding-manifest-v1",
                "tenant_id": str(tenant_id),
                "document_version_id": str(document_version_id),
                "provider": provider.name,
                "model_name": provider.model_name,
                "model_version": provider.model_version,
                "dimensions": provider.dimensions,
            },
        )
        for chunk in iter_chunk_manifest(
            chunk_manifest_path, tenant_id, document_version_id
        ):
            pending.append(chunk)
            if len(pending) == batch_size:
                count += _write_batch(output, pending, provider)
                pending.clear()
        if pending:
            count += _write_batch(output, pending, provider)
        if count == 0:
            raise ValueError("chunk manifest contains no embeddable records")
        _write_line(output, {"record_type": "manifest", "embedding_count": count})
    return count


def iter_embedding_manifest(
    path: Path,
    tenant_id: UUID,
    document_version_id: UUID,
) -> Iterator[EmbeddingRecord]:
    count = 0
    declared_count: int | None = None
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            record = json.loads(line)
            if line_number == 1 and record.get("record_type") != "header":
                raise ValueError("embedding manifest header is missing")
            if record.get("tenant_id") not in {None, str(tenant_id)}:
                raise ValueError("embedding manifest tenant does not match")
            if record.get("document_version_id") not in {
                None,
                str(document_version_id),
            }:
                raise ValueError("embedding manifest version does not match")
            if record.get("record_type") == "embedding":
                embedded = EmbeddedChunk.model_validate(record.get("embedding"))
                count += 1
                yield EmbeddingRecord(
                    chunk_id=embedded.chunk_id,
                    checksum_sha256=embedded.checksum_sha256,
                )
            elif record.get("record_type") == "manifest":
                declared_count = int(record.get("embedding_count", -1))
    if declared_count != count:
        raise ValueError("embedding manifest count does not match records")


def load_embeddings(
    path: Path,
    tenant_id: UUID,
    document_version_id: UUID,
    maximum_records: int,
) -> dict[UUID, EmbeddedChunk]:
    embeddings: dict[UUID, EmbeddedChunk] = {}
    with path.open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if record.get("tenant_id") not in {None, str(tenant_id)}:
                raise ValueError("embedding manifest tenant does not match")
            if record.get("document_version_id") not in {
                None,
                str(document_version_id),
            }:
                raise ValueError("embedding manifest version does not match")
            if record.get("record_type") != "embedding":
                continue
            embedded = EmbeddedChunk.model_validate(record.get("embedding"))
            embeddings[embedded.chunk_id] = embedded
            if len(embeddings) > maximum_records:
                raise ValueError("embedding manifest exceeds configured record limit")
    return embeddings


def _write_batch(
    output: TextIO,
    chunks: list[ChunkRecord],
    provider: EmbeddingProvider,
) -> int:
    vectors = provider.embed([chunk.text for chunk in chunks])
    for chunk, vector in zip(chunks, vectors, strict=True):
        checksum = hashlib.sha256(
            struct.pack(f"<{len(vector)}f", *vector)
        ).hexdigest()
        _write_line(
            output,
            {
                "record_type": "embedding",
                "embedding": {
                    "chunk_id": str(chunk.chunk_id),
                    "vector": vector,
                    "checksum_sha256": checksum,
                },
            },
        )
    return len(chunks)


def _write_line(output: TextIO, record: dict[str, object]) -> None:
    json.dump(record, output, ensure_ascii=False, separators=(",", ":"))
    output.write("\n")
