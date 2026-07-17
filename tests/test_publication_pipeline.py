from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import pytest

from app.core.config import EmbeddingSettings, IngestionPolicySettings
from app.services.ingestion.artifacts import PageArtifactWriter
from app.services.ingestion.chunking import iter_chunk_manifest, write_chunk_manifest
from app.services.ingestion.embedding_artifacts import (
    iter_embedding_manifest,
    load_embeddings,
    write_embedding_manifest,
)
from app.services.ingestion.embedding_provider import FastEmbedProvider
from app.services.ingestion.models import (
    DocumentRoute,
    InspectionReport,
    NormalizedPage,
    PageOrigin,
    TextBlock,
)

TENANT_ID = UUID("00000000-0000-0000-0000-000000000071")
VERSION_ID = UUID("00000000-0000-0000-0000-000000000073")


class WordSpans:
    def spans(self, text: str) -> tuple[tuple[int, int], ...]:
        return tuple(match.span() for match in re.finditer(r"\S+", text))


class RecordingEmbeddingProvider:
    name = "test-provider"
    model_name = "test-model"
    model_version = "v1"
    dimensions = 2

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.batch_sizes.append(len(texts))
        return [[float(len(text)), 1.0] for text in texts]


def _normalized_artifact(path: Path) -> None:
    writer = PageArtifactWriter(path, VERSION_ID, "test", "v1")
    pages = (
        NormalizedPage(
            page_number=1,
            origin=PageOrigin.NATIVE,
            text=" ".join(f"word{number}" for number in range(1, 26)),
            blocks=(
                TextBlock(
                    ordinal=0,
                    text=" ".join(f"word{number}" for number in range(1, 26)),
                ),
            ),
            language_hint="eng",
            quality_score=1,
        ),
        NormalizedPage(
            page_number=2,
            origin=PageOrigin.NATIVE,
            text="eight nine ten",
            blocks=(TextBlock(ordinal=0, text="eight nine ten"),),
            language_hint="eng",
            quality_score=1,
        ),
    )
    for page in pages:
        writer.write_page(page)
    writer.finish(
        InspectionReport(
            parser_name="test",
            parser_version="v1",
            page_count=2,
            route=DocumentRoute.NATIVE,
            native_page_count=2,
            ocr_page_numbers=(),
            extracted_text_chars=sum(len(page.text) for page in pages),
        )
    )


def test_chunking_is_deterministic_bounded_and_page_scoped(tmp_path: Path) -> None:
    source = tmp_path / "normalized.jsonl"
    first = tmp_path / "chunks-first.jsonl"
    second = tmp_path / "chunks-second.jsonl"
    _normalized_artifact(source)
    policy = IngestionPolicySettings(
        chunk_max_tokens=16,
        chunk_overlap_tokens=4,
        max_chunks=10,
    )

    first_count = write_chunk_manifest(
        source, first, TENANT_ID, VERSION_ID, policy, WordSpans()
    )
    second_count = write_chunk_manifest(
        source, second, TENANT_ID, VERSION_ID, policy, WordSpans()
    )
    first_chunks = list(iter_chunk_manifest(first, TENANT_ID, VERSION_ID))
    second_chunks = list(iter_chunk_manifest(second, TENANT_ID, VERSION_ID))

    assert first_count == second_count == 3
    assert first_chunks == second_chunks
    assert [chunk.ordinal for chunk in first_chunks] == [0, 1, 2]
    assert all(chunk.token_count <= 16 for chunk in first_chunks)
    assert all(chunk.page_start == chunk.page_end for chunk in first_chunks)
    assert first_chunks[0].text.endswith("word16")
    assert first_chunks[1].text.startswith("word13")
    assert first_chunks[0].block_bounds[0]["char_start"] == 0


def test_embedding_manifest_caps_batches_and_preserves_coverage(
    tmp_path: Path,
) -> None:
    source = tmp_path / "normalized.jsonl"
    chunks_path = tmp_path / "chunks.jsonl"
    embeddings_path = tmp_path / "embeddings.jsonl"
    _normalized_artifact(source)
    policy = IngestionPolicySettings(
        chunk_max_tokens=16,
        chunk_overlap_tokens=4,
        max_chunks=10,
    )
    write_chunk_manifest(
        source, chunks_path, TENANT_ID, VERSION_ID, policy, WordSpans()
    )
    provider = RecordingEmbeddingProvider()

    count = write_embedding_manifest(
        chunks_path,
        embeddings_path,
        TENANT_ID,
        VERSION_ID,
        provider,
        batch_size=2,
    )
    records = list(iter_embedding_manifest(embeddings_path, TENANT_ID, VERSION_ID))
    loaded = load_embeddings(embeddings_path, TENANT_ID, VERSION_ID, 10)

    assert count == len(records) == len(loaded) == 3
    assert provider.batch_sizes == [2, 1]
    assert all(len(record.vector) == 2 for record in loaded.values())


def test_embedding_provider_rejects_unverified_model(tmp_path: Path) -> None:
    (tmp_path / "model_optimized.onnx").write_bytes(b"not-the-selected-model")

    with pytest.raises(RuntimeError, match="checksum"):
        FastEmbedProvider(
            EmbeddingSettings(model_directory=str(tmp_path), model_sha256="0" * 64)
        )
