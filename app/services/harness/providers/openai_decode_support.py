"""Validation and classification helpers for OpenAI stream decoding."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import cast

from app.services.harness.protocol import ProviderFailureClass

MAXIMUM_OPENAI_WIRE_EVENT_BYTES = 64 * 1024
MAXIMUM_OPENAI_WIRE_EVENTS = 4_096
MAXIMUM_OPENAI_FUNCTION_CALLS = 256


class OpenAIDecodeErrorCode(StrEnum):
    EVENT_LIMIT = "event_limit"
    EVENT_SIZE = "event_size"
    MALFORMED = "malformed"
    SEQUENCE = "sequence"
    TERMINAL = "terminal"
    UNSUPPORTED = "unsupported"


class OpenAIDecodeError(ValueError):
    def __init__(self, code: OpenAIDecodeErrorCode) -> None:
        super().__init__("OpenAI Responses stream decoding failed")
        self.code = code


def classify_error(code: str) -> tuple[ProviderFailureClass, bool]:
    normalized = code.lower()
    if "rate_limit" in normalized:
        return ProviderFailureClass.RATE_LIMIT, True
    if normalized in {"server_error", "overloaded", "service_unavailable"}:
        return ProviderFailureClass.TRANSIENT, True
    if normalized in {
        "context_length_exceeded",
        "max_context_length",
        "max_prompt_tokens",
    }:
        return ProviderFailureClass.CONTEXT_LENGTH, False
    if normalized in {
        "authentication_error",
        "invalid_api_key",
        "invalid_authentication",
    }:
        return ProviderFailureClass.AUTHENTICATION, False
    if normalized in {"content_filter", "policy_violation", "safety_violation"}:
        return ProviderFailureClass.POLICY, False
    return ProviderFailureClass.INTERNAL, False


def canonical_arguments(arguments: str) -> str:
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED) from None
    if not isinstance(parsed, dict):
        raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED)
    try:
        return json.dumps(
            parsed,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED) from None


def mapping(value: dict[str, object], key: str) -> dict[str, object]:
    nested = value.get(key)
    if not isinstance(nested, dict):
        raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED)
    return cast(dict[str, object], nested)


def string(
    value: dict[str, object],
    key: str,
    *,
    maximum: int,
) -> str:
    field = value.get(key)
    if not isinstance(field, str) or not 1 <= len(field) <= maximum:
        raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED)
    return field


def integer(value: dict[str, object], key: str) -> int:
    field = value.get(key)
    if not isinstance(field, int) or isinstance(field, bool) or field < 0:
        raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED)
    return field


def optional_nested_integer(
    value: dict[str, object],
    nested_key: str,
    key: str,
) -> int:
    nested = value.get(nested_key)
    if nested is None:
        return 0
    if not isinstance(nested, dict):
        raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED)
    return integer(cast(dict[str, object], nested), key)
