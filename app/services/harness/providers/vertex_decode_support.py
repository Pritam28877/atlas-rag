"""Validation and classification helpers for Vertex stream decoding."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from app.services.harness.protocol import ProviderFailureClass

MAXIMUM_VERTEX_WIRE_EVENT_BYTES = 256 * 1024
MAXIMUM_VERTEX_WIRE_EVENTS = 4_096
MAXIMUM_VERTEX_FUNCTION_CALLS = 256
MAXIMUM_VERTEX_TOOL_ARGUMENT_BYTES = 128 * 1024

POLICY_FINISH_REASONS = frozenset(
    {
        "BLOCKLIST",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_SAFETY",
        "MODEL_ARMOR",
        "PROHIBITED_CONTENT",
        "RECITATION",
        "SAFETY",
        "SPII",
    }
)
MALFORMED_FINISH_REASONS = frozenset(
    {
        "MALFORMED_FUNCTION_CALL",
        "NO_IMAGE",
        "OTHER",
        "UNEXPECTED_TOOL_CALL",
    }
)


class VertexDecodeErrorCode(StrEnum):
    EVENT_LIMIT = "event_limit"
    EVENT_SIZE = "event_size"
    MALFORMED = "malformed"
    SEQUENCE = "sequence"
    TERMINAL = "terminal"
    UNSUPPORTED = "unsupported"


class VertexDecodeError(ValueError):
    def __init__(self, code: VertexDecodeErrorCode) -> None:
        super().__init__("Vertex GenerateContent stream decoding failed")
        self.code = code


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED) from None


def mapping(value: dict[str, object], key: str) -> dict[str, object]:
    nested = value.get(key)
    if not isinstance(nested, dict):
        raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED)
    return cast(dict[str, object], nested)


def sequence(value: dict[str, object], key: str) -> list[object]:
    nested = value.get(key)
    if not isinstance(nested, list):
        raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED)
    return cast(list[object], nested)


def string(
    value: dict[str, object],
    key: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    field = value.get(key)
    minimum = 0 if allow_empty else 1
    if (
        not isinstance(field, str)
        or not minimum <= len(field) <= maximum
    ):
        raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED)
    return field


def integer(
    value: dict[str, object],
    key: str,
    *,
    default: int | None = None,
) -> int:
    field = value.get(key, default)
    if (
        not isinstance(field, int)
        or isinstance(field, bool)
        or not 0 <= field <= 2_000_000
    ):
        raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED)
    return field


@dataclass(frozen=True, slots=True)
class VertexUsageCounts:
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    tool_tokens: int
    total_tokens: int


def parse_usage(usage: dict[str, object]) -> VertexUsageCounts:
    counts = VertexUsageCounts(
        input_tokens=integer(usage, "promptTokenCount"),
        output_tokens=integer(usage, "candidatesTokenCount", default=0),
        cached_tokens=integer(usage, "cachedContentTokenCount", default=0),
        reasoning_tokens=integer(usage, "thoughtsTokenCount", default=0),
        tool_tokens=integer(usage, "toolUsePromptTokenCount", default=0),
        total_tokens=integer(usage, "totalTokenCount"),
    )
    expected_total = (
        counts.input_tokens
        + counts.output_tokens
        + counts.reasoning_tokens
        + counts.tool_tokens
    )
    if (
        counts.cached_tokens > counts.input_tokens
        or counts.total_tokens != expected_total
    ):
        raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED)
    return counts


def require_allowed_keys(
    value: dict[str, object],
    allowed: frozenset[str],
) -> None:
    if not set(value).issubset(allowed):
        raise VertexDecodeError(VertexDecodeErrorCode.UNSUPPORTED)


def classify_api_error(
    status: str,
) -> tuple[ProviderFailureClass, bool]:
    if status == "RESOURCE_EXHAUSTED":
        return ProviderFailureClass.RATE_LIMIT, True
    if status in {
        "ABORTED",
        "DEADLINE_EXCEEDED",
        "INTERNAL",
        "UNAVAILABLE",
    }:
        return ProviderFailureClass.TRANSIENT, True
    if status in {"PERMISSION_DENIED", "UNAUTHENTICATED"}:
        return ProviderFailureClass.AUTHENTICATION, False
    if status in {"INVALID_ARGUMENT", "FAILED_PRECONDITION"}:
        return ProviderFailureClass.MALFORMED, False
    return ProviderFailureClass.INTERNAL, False
