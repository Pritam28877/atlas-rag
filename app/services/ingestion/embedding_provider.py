"""Provider-neutral, checksum-pinned local embedding execution."""

from __future__ import annotations

import hashlib
import warnings
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path
from typing import Protocol

from app.core.config import EmbeddingSettings


class EmbeddingProvider(Protocol):
    name: str
    model_name: str
    model_version: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class FastEmbedProvider:
    name = "fastembed"

    def __init__(self, settings: EmbeddingSettings) -> None:
        from fastembed import TextEmbedding

        if version("fastembed") != "0.8.0":
            raise RuntimeError("FastEmbed runtime version does not match configuration")
        model_directory = Path(settings.model_directory)
        model_path = model_directory / "model_optimized.onnx"
        if not model_path.is_file():
            raise RuntimeError("configured embedding model artifact is unavailable")
        if _file_sha256(model_path) != settings.model_sha256:
            raise RuntimeError("embedding model checksum does not match configuration")
        self.model_name = settings.model_name
        self.model_version = settings.model_version
        self.dimensions = settings.dimensions
        self._batch_size = settings.batch_size
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=".*now uses mean pooling instead of CLS embedding.*",
                category=UserWarning,
            )
            self._model = TextEmbedding(
                model_name=settings.model_name,
                specific_model_path=str(model_directory),
                local_files_only=True,
                threads=2,
                providers=["CPUExecutionProvider"],
            )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts or len(texts) > self._batch_size:
            raise ValueError("embedding batch is empty or exceeds configured limit")
        vectors = self._model.embed(texts, batch_size=self._batch_size, parallel=None)
        normalized = [vector.astype(float, copy=False).tolist() for vector in vectors]
        if len(normalized) != len(texts):
            raise RuntimeError("embedding provider returned an incomplete batch")
        if any(len(vector) != self.dimensions for vector in normalized):
            raise RuntimeError("embedding provider returned an invalid dimension")
        return normalized


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while payload := source.read(1024 * 1024):
            digest.update(payload)
    return digest.hexdigest()
