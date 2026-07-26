"""Policy-bound OpenRouter request and routing metadata contracts."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol import Sha256, StrictProtocolModel
from app.services.harness.providers.openai_contracts import (
    CompiledOpenAIResponsesRequest,
)

type OpenRouterProviderSlug = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9]+(?:[./_-][a-z0-9]+)*$",
    ),
]

SENSITIVE_ROUTING_METADATA_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "completion",
        "credential",
        "input",
        "output",
        "prompt",
        "secret",
        "token",
    }
)


class OpenRouterProviderPolicy(StrictProtocolModel):
    order: tuple[OpenRouterProviderSlug, ...] = Field(
        min_length=1,
        max_length=16,
    )
    only: tuple[OpenRouterProviderSlug, ...] = Field(
        min_length=1,
        max_length=16,
    )
    allow_fallbacks: Literal[False] = False
    require_parameters: Literal[True] = True
    data_collection: Literal["deny"] = "deny"
    zdr: Literal[True] = True

    @model_validator(mode="after")
    def validate_policy_binding(self) -> Self:
        if self.order != self.only:
            raise ValueError("OpenRouter order must equal its Atlas allowlist")
        if tuple(dict.fromkeys(self.only)) != self.only:
            raise ValueError("OpenRouter provider allowlist must be unique")
        return self


class CompiledOpenRouterResponsesRequest(CompiledOpenAIResponsesRequest):
    provider: OpenRouterProviderPolicy


class OpenRouterRoutingMetadata(StrictProtocolModel):
    canonical_json: str = Field(min_length=2, max_length=16 * 1024)
    content_sha256: Sha256

    @model_validator(mode="after")
    def validate_redacted_canonical_metadata(self) -> Self:
        try:
            metadata = json.loads(self.canonical_json)
        except json.JSONDecodeError as error:
            raise ValueError("OpenRouter metadata must be valid JSON") from error
        if not isinstance(metadata, dict):
            raise ValueError("OpenRouter metadata must be a JSON object")
        canonical = json.dumps(
            metadata,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if canonical != self.canonical_json:
            raise ValueError("OpenRouter metadata must use canonical JSON")
        _validate_metadata_values(metadata)
        digest = hashlib.sha256(self.canonical_json.encode()).hexdigest()
        if digest != self.content_sha256:
            raise ValueError("OpenRouter metadata hash mismatch")
        return self


def compile_routing_metadata(
    metadata: dict[str, object],
) -> OpenRouterRoutingMetadata:
    _validate_metadata_values(metadata)
    try:
        canonical = json.dumps(
            metadata,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        raise ValueError("OpenRouter metadata is not bounded JSON") from None
    return OpenRouterRoutingMetadata(
        canonical_json=canonical,
        content_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def _validate_metadata_values(metadata: dict[str, object]) -> None:
    pending: list[object] = [metadata]
    visited = 0
    while pending:
        current = pending.pop()
        visited += 1
        if visited > 1_024:
            raise ValueError("OpenRouter metadata exceeds its node limit")
        if isinstance(current, dict):
            for key, value in current.items():
                if (
                    not isinstance(key, str)
                    or not 1 <= len(key) <= 128
                    or key.lower() in SENSITIVE_ROUTING_METADATA_KEYS
                ):
                    raise ValueError("OpenRouter metadata contains unsafe keys")
                pending.append(value)
        elif isinstance(current, list):
            if len(current) > 256:
                raise ValueError("OpenRouter metadata list is too large")
            pending.extend(current)
        elif isinstance(current, str):
            if len(current) > 1_024:
                raise ValueError("OpenRouter metadata string is too large")
        elif current is not None and not isinstance(
            current, (bool, int, float)
        ):
            raise ValueError("OpenRouter metadata contains unsupported values")
