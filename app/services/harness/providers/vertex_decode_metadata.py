"""Hashed Vertex response metadata that never retains opaque provider values."""

from __future__ import annotations

import hashlib

from app.services.harness.providers.vertex_contracts import VertexStreamMetadata
from app.services.harness.providers.vertex_decode_support import (
    VertexDecodeError,
    VertexDecodeErrorCode,
    VertexUsageCounts,
    canonical_json,
    string,
)


class VertexMetadataAccumulator:
    def __init__(self) -> None:
        self._response_id_sha256: str | None = None
        self._model_version_sha256: str | None = None
        self._thought_signatures = hashlib.sha256()
        self._has_thought_signatures = False
        self._safety_metadata = hashlib.sha256()
        self._has_safety_metadata = False
        self.metadata: VertexStreamMetadata | None = None

    @property
    def response_id_sha256(self) -> str | None:
        return self._response_id_sha256

    def record_identity(self, response: dict[str, object]) -> None:
        self._response_id_sha256 = self._stable_digest(
            response,
            "responseId",
            self._response_id_sha256,
        )
        self._model_version_sha256 = self._stable_digest(
            response,
            "modelVersion",
            self._model_version_sha256,
        )

    def record_thought_signature(self, signature: str) -> None:
        encoded = signature.encode()
        self._thought_signatures.update(len(encoded).to_bytes(4, "big"))
        self._thought_signatures.update(encoded)
        self._has_thought_signatures = True

    def record_safety(self, value: object) -> None:
        encoded = canonical_json(value).encode()
        self._safety_metadata.update(len(encoded).to_bytes(4, "big"))
        self._safety_metadata.update(encoded)
        self._has_safety_metadata = True

    def finalize(
        self,
        finish_reason: str,
        usage: VertexUsageCounts,
    ) -> None:
        self.metadata = VertexStreamMetadata(
            finish_reason=finish_reason,
            response_id_sha256=self._response_id_sha256,
            model_version_sha256=self._model_version_sha256,
            thought_signature_sha256=(
                self._thought_signatures.hexdigest()
                if self._has_thought_signatures
                else None
            ),
            safety_metadata_sha256=(
                self._safety_metadata.hexdigest()
                if self._has_safety_metadata
                else None
            ),
            total_tokens=usage.total_tokens,
            tool_use_prompt_tokens=usage.tool_tokens,
        )

    @staticmethod
    def _stable_digest(
        response: dict[str, object],
        key: str,
        existing: str | None,
    ) -> str | None:
        value = response.get(key)
        if value is None:
            return existing
        raw = string(response, key, maximum=2_048)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        if existing is not None and digest != existing:
            raise VertexDecodeError(VertexDecodeErrorCode.SEQUENCE)
        return digest
