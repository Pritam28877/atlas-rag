"""Deterministic context manifest compilation with bounded references."""

from __future__ import annotations

import hashlib
import json
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    ContextId,
    PolicyVersion,
    Sha256,
    StrictProtocolModel,
    TurnId,
    UtcTimestamp,
)
from app.services.harness.protocol.conversation import (
    ContextManifest,
    ContextOmission,
    ContextOmissionKind,
    ContextSourceReference,
)

MAXIMUM_CONTEXT_CANDIDATES = 512


class ContextCandidate(StrictProtocolModel):
    reference: ContextSourceReference
    required: bool = False


class ContextCompileRequest(StrictProtocolModel):
    context_id: ContextId
    turn_id: TurnId
    provider_policy_version: PolicyVersion
    context_window_tokens: int = Field(ge=1, le=2_000_000)
    reserved_output_tokens: int = Field(ge=1, le=512_000)
    reserved_reasoning_tokens: int = Field(ge=0, le=512_000)
    reserved_tool_tokens: int = Field(ge=0, le=512_000)
    candidates: tuple[ContextCandidate, ...] = Field(
        max_length=MAXIMUM_CONTEXT_CANDIDATES
    )
    compiled_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_candidates(self) -> Self:
        source_ids = tuple(
            candidate.reference.source_id for candidate in self.candidates
        )
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("context candidates must have unique source IDs")
        reserved = (
            self.reserved_output_tokens
            + self.reserved_reasoning_tokens
            + self.reserved_tool_tokens
        )
        if reserved >= self.context_window_tokens:
            raise ValueError("context reservations leave no input capacity")
        return self


class ContextCompilation(StrictProtocolModel):
    manifest: ContextManifest
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        expected = _manifest_hash(self.manifest)
        if expected != self.manifest_sha256:
            raise ValueError("context manifest hash is invalid")
        return self


class ContextBudgetError(ValueError):
    pass


class ContextCompiler:
    """Select only references that fit after hard output reservations."""

    def compile(self, request: ContextCompileRequest) -> ContextCompilation:
        verified = ContextCompileRequest.model_validate(request.model_dump())
        reserved = (
            verified.reserved_output_tokens
            + verified.reserved_reasoning_tokens
            + verified.reserved_tool_tokens
        )
        available = verified.context_window_tokens - reserved
        ordered = tuple(
            sorted(
                verified.candidates,
                key=lambda candidate: (
                    not candidate.required,
                    -candidate.reference.priority,
                    candidate.reference.source_id,
                ),
            )
        )
        selected: list[ContextSourceReference] = []
        omissions: list[ContextOmission] = []
        selected_tokens = 0
        for candidate in ordered:
            reference = candidate.reference
            if (
                candidate.required
                and selected_tokens + reference.token_count > available
            ):
                raise ContextBudgetError(
                    f"required context source does not fit: {reference.source_id}"
                )
            if selected_tokens + reference.token_count <= available:
                selected.append(reference)
                selected_tokens += reference.token_count
                continue
            omissions.append(
                ContextOmission(
                    source_id=reference.source_id,
                    kind=ContextOmissionKind.TOKEN_BUDGET,
                    reason="source omitted after hard context reservations",
                )
            )
        manifest = ContextManifest(
            context_id=verified.context_id,
            turn_id=verified.turn_id,
            provider_policy_version=verified.provider_policy_version,
            context_window_tokens=verified.context_window_tokens,
            selected_token_count=selected_tokens,
            reserved_output_tokens=verified.reserved_output_tokens,
            reserved_reasoning_tokens=verified.reserved_reasoning_tokens,
            reserved_tool_tokens=verified.reserved_tool_tokens,
            sources=tuple(selected),
            omissions=tuple(omissions),
            compiled_at=verified.compiled_at,
        )
        return ContextCompilation(
            manifest=manifest,
            manifest_sha256=_manifest_hash(manifest),
        )


def _manifest_hash(manifest: ContextManifest) -> str:
    encoded = json.dumps(
        manifest.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
