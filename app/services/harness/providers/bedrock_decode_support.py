"""Validation helpers for the Bedrock ConverseStream decoder."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import cast

from app.services.harness.protocol import (
    ProviderFailureClass,
    ProviderFinishReason,
)

MAXIMUM_BEDROCK_EVENTS = 4_096
MAXIMUM_BEDROCK_EVENT_BYTES = 64 * 1024
MAXIMUM_BEDROCK_BLOCKS = 256
MAXIMUM_BEDROCK_TOOL_ARGUMENT_BYTES = 128 * 1024

FINISH_REASONS = {
    "end_turn": ProviderFinishReason.STOP,
    "max_tokens": ProviderFinishReason.LENGTH,
    "stop_sequence": ProviderFinishReason.STOP,
    "tool_use": ProviderFinishReason.TOOL_CALLS,
}
STREAM_ERROR_CLASSIFICATION = {
    "throttlingException": (ProviderFailureClass.RATE_LIMIT, True),
    "serviceUnavailableException": (ProviderFailureClass.TRANSIENT, True),
    "internalServerException": (ProviderFailureClass.INTERNAL, False),
    "modelStreamErrorException": (ProviderFailureClass.INTERNAL, False),
    "validationException": (ProviderFailureClass.MALFORMED, False),
}


class BedrockDecodeErrorCode(StrEnum):
    EVENT_LIMIT = "event_limit"
    EVENT_SIZE = "event_size"
    MALFORMED = "malformed"
    SEQUENCE = "sequence"
    TERMINAL = "terminal"
    UNSUPPORTED = "unsupported"


class BedrockDecodeError(ValueError):
    def __init__(self, code: BedrockDecodeErrorCode) -> None:
        super().__init__("Bedrock ConverseStream event decoding failed")
        self.code = code


def mapping(values: dict[str, object], key: str) -> dict[str, object]:
    value = values.get(key)
    if not isinstance(value, dict):
        raise BedrockDecodeError(BedrockDecodeErrorCode.MALFORMED)
    return cast(dict[str, object], value)


def string(values: dict[str, object], key: str, maximum: int) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise BedrockDecodeError(BedrockDecodeErrorCode.MALFORMED)
    return value


def integer(values: dict[str, object], key: str) -> int:
    value = values.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BedrockDecodeError(BedrockDecodeErrorCode.MALFORMED)
    return value


def optional_integer(values: dict[str, object], key: str) -> int:
    return 0 if key not in values else integer(values, key)


def content_index(payload: dict[str, object]) -> int:
    index = integer(payload, "contentBlockIndex")
    if index >= MAXIMUM_BEDROCK_BLOCKS:
        raise BedrockDecodeError(BedrockDecodeErrorCode.MALFORMED)
    return index


def canonical_arguments(value: str) -> str:
    try:
        arguments = json.loads(value)
    except json.JSONDecodeError:
        raise BedrockDecodeError(BedrockDecodeErrorCode.MALFORMED) from None
    if not isinstance(arguments, dict):
        raise BedrockDecodeError(BedrockDecodeErrorCode.MALFORMED)
    return json.dumps(
        arguments,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def metadata_sha256(metadata: dict[str, object]) -> str | None:
    if not metadata:
        return None
    try:
        encoded = json.dumps(
            metadata,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError):
        raise BedrockDecodeError(BedrockDecodeErrorCode.MALFORMED) from None
    if len(encoded) > MAXIMUM_BEDROCK_EVENT_BYTES:
        raise BedrockDecodeError(BedrockDecodeErrorCode.EVENT_SIZE)
    return hashlib.sha256(encoded).hexdigest()
