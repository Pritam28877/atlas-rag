"""Deterministic, page-aware chunk generation with bounded per-page memory."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Protocol, TextIO
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import EmbeddingSettings, IngestionPolicySettings
from app.services.ingestion.artifacts import iter_page_artifact
from app.services.ingestion.models import NormalizedPage

CHUNK_NAMESPACE = UUID("7f901dc8-6e57-53ad-a14b-e5aeb5ea407d")
CHUNKER_NAME = "page-token-window"
CHUNKER_VERSION = "v1"


class TokenSpanProvider(Protocol):
    def spans(self, text: str) -> Sequence[tuple[int, int]]: ...


class FastEmbedTokenSpanProvider:
    def __init__(self, settings: EmbeddingSettings) -> None:
        from tokenizers import Tokenizer

        tokenizer_path = Path(settings.model_directory) / "tokenizer.json"
        if not tokenizer_path.is_file():
            raise RuntimeError("configured embedding tokenizer is unavailable")
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))

    def spans(self, text: str) -> Sequence[tuple[int, int]]:
        encoding = self._tokenizer.encode(text, add_special_tokens=False)
        return tuple((start, end) for start, end in encoding.offsets if end > start)


class ChunkRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: UUID
    ordinal: int = Field(ge=0)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    text: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    token_count: int = Field(gt=0)
    section_path: tuple[str, ...] = ()
    block_bounds: tuple[dict[str, object], ...] = ()
    chunker_name: str = CHUNKER_NAME
    chunker_version: str = CHUNKER_VERSION


def write_chunk_manifest(
    normalized_path: Path,
    destination_path: Path,
    tenant_id: UUID,
    document_version_id: UUID,
    policy: IngestionPolicySettings,
    token_spans: TokenSpanProvider,
    source_document_version_id: UUID | None = None,
) -> int:
    chunk_count = 0
    with destination_path.open("x", encoding="utf-8") as output:
        _write_line(
            output,
            {
                "record_type": "header",
                "schema_version": "chunk-manifest-v1",
                "tenant_id": str(tenant_id),
                "document_version_id": str(document_version_id),
                "chunker_name": CHUNKER_NAME,
                "chunker_version": CHUNKER_VERSION,
                "max_tokens": policy.chunk_max_tokens,
                "overlap_tokens": policy.chunk_overlap_tokens,
            },
        )
        source_version_id = source_document_version_id or document_version_id
        for page in iter_page_artifact(normalized_path, source_version_id):
            for chunk in _chunk_page(
                page,
                tenant_id,
                document_version_id,
                chunk_count,
                policy,
                token_spans,
            ):
                _write_line(
                    output,
                    {
                        "record_type": "chunk",
                        "tenant_id": str(tenant_id),
                        "document_version_id": str(document_version_id),
                        "chunk": chunk.model_dump(mode="json"),
                    },
                )
                chunk_count += 1
                if chunk_count > policy.max_chunks:
                    raise ValueError("document exceeds configured chunk limit")
        if chunk_count == 0:
            raise ValueError("normalized document contains no searchable text")
        _write_line(output, {"record_type": "manifest", "chunk_count": chunk_count})
    return chunk_count


def iter_chunk_manifest(
    path: Path,
    tenant_id: UUID,
    document_version_id: UUID,
) -> Iterator[ChunkRecord]:
    count = 0
    declared_count: int | None = None
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            record = json.loads(line)
            if line_number == 1 and record.get("record_type") != "header":
                raise ValueError("chunk manifest header is missing")
            if record.get("tenant_id") not in {None, str(tenant_id)}:
                raise ValueError("chunk manifest tenant does not match")
            if record.get("document_version_id") not in {
                None,
                str(document_version_id),
            }:
                raise ValueError("chunk manifest version does not match")
            if record.get("record_type") == "chunk":
                chunk = ChunkRecord.model_validate(record.get("chunk"))
                if chunk.ordinal != count:
                    raise ValueError("chunk manifest ordinal is invalid")
                count += 1
                yield chunk
            elif record.get("record_type") == "manifest":
                declared_count = int(record.get("chunk_count", -1))
    if declared_count != count:
        raise ValueError("chunk manifest count does not match records")


def _chunk_page(
    page: NormalizedPage,
    tenant_id: UUID,
    document_version_id: UUID,
    first_ordinal: int,
    policy: IngestionPolicySettings,
    token_spans: TokenSpanProvider,
) -> Iterator[ChunkRecord]:
    text = page.text
    if not text.strip():
        return
    if len(text) > policy.chunk_page_max_chars:
        raise ValueError("page text exceeds configured chunking limit")
    spans = token_spans.spans(text)
    step = policy.chunk_max_tokens - policy.chunk_overlap_tokens
    for window_start in range(0, len(spans), step):
        window = spans[window_start : window_start + policy.chunk_max_tokens]
        if not window:
            break
        char_start = window[0][0]
        char_end = window[-1][1]
        chunk_text = text[char_start:char_end]
        if not chunk_text:
            continue
        content_sha256 = hashlib.sha256(chunk_text.encode()).hexdigest()
        ordinal = first_ordinal + window_start // step
        identity = (
            f"{tenant_id}:{document_version_id}:{CHUNKER_NAME}:{CHUNKER_VERSION}:"
            f"{ordinal}:{content_sha256}"
        )
        yield ChunkRecord(
            chunk_id=uuid5(CHUNK_NAMESPACE, identity),
            ordinal=ordinal,
            page_start=page.page_number,
            page_end=page.page_number,
            char_start=char_start,
            char_end=char_end,
            text=chunk_text,
            content_sha256=content_sha256,
            token_count=len(window),
            block_bounds=_block_bounds(page, char_start, char_end),
        )
        if window_start + policy.chunk_max_tokens >= len(spans):
            break


def _block_bounds(
    page: NormalizedPage, char_start: int, char_end: int
) -> tuple[dict[str, object], ...]:
    bounds: list[dict[str, object]] = []
    cursor = 0
    for block in page.blocks:
        block_start = page.text.find(block.text, cursor)
        if block_start < 0:
            continue
        block_end = block_start + len(block.text)
        cursor = block_end
        if block_end <= char_start or block_start >= char_end:
            continue
        bounds.append(
            {
                "block_ordinal": block.ordinal,
                "char_start": max(block_start, char_start),
                "char_end": min(block_end, char_end),
                "bounds": block.bounds.model_dump() if block.bounds else None,
            }
        )
    return tuple(bounds)


def _write_line(output: TextIO, record: dict[str, object]) -> None:
    json.dump(record, output, ensure_ascii=False, separators=(",", ":"))
    output.write("\n")
