"""Bounded embedding profiles used by the fixture-derived retrieval benchmark."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

TOKEN_HASH_DIMENSION = 64
MULTILINGUAL_MODEL_NAME = (
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
MULTILINGUAL_MODEL_VERSION = (
    "faf4aa4225822f3bc6376869cb1164e8e3feedd0-fastembed-0.8.0-mean"
)


class EmbeddingProfile(Protocol):
    name: str
    model_version: str
    dimension: int
    license_name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class TokenHashEmbeddingProfile:
    name = "unicode-token-hash-v1"
    model_version = "v1"
    dimension = TOKEN_HASH_DIMENSION
    license_name = "repository-control"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [_token_hash_embedding(text) for text in texts]


class MultilingualMiniLmEmbeddingProfile:
    name = MULTILINGUAL_MODEL_NAME
    model_version = MULTILINGUAL_MODEL_VERSION
    dimension = 384
    license_name = "Apache-2.0"

    def __init__(
        self,
        cache_dir: Path | None = None,
        threads: int = 2,
        batch_size: int = 16,
    ) -> None:
        if threads < 1 or batch_size < 1:
            raise ValueError("embedding threads and batch size must be positive")
        try:
            from fastembed import TextEmbedding
        except ImportError as error:
            raise RuntimeError(
                "install the embedding-benchmark dependency group"
            ) from error
        self._batch_size = batch_size
        self._model = TextEmbedding(
            model_name=self.name,
            cache_dir=str(cache_dir) if cache_dir else None,
            threads=threads,
            providers=["CPUExecutionProvider"],
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._model.embed(texts, batch_size=self._batch_size, parallel=None)
        return [vector.astype(float, copy=False).tolist() for vector in vectors]


def _token_hash_embedding(text: str) -> list[float]:
    values = [0.0] * TOKEN_HASH_DIMENSION
    for token in re.findall(r"\w+", text.casefold(), flags=re.UNICODE):
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % TOKEN_HASH_DIMENSION
        values[index] += 1.0 if digest[4] % 2 else -1.0
    magnitude = math.sqrt(sum(value * value for value in values))
    return [value / magnitude for value in values] if magnitude else values
