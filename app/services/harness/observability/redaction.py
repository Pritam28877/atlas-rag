"""Recursive, fail-safe redaction before any harness telemetry sink."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

MAXIMUM_REDACTION_DEPTH = 8
MAXIMUM_REDACTION_ENTRIES = 128
MAXIMUM_REDACTION_ITEMS = 64
MAXIMUM_REDACTION_STRING_BYTES = 512
REDACTED_VALUE = "<redacted>"
TRUNCATED_VALUE = "<truncated>"
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "content",
    "cookie",
    "credential",
    "email",
    "file",
    "header",
    "password",
    "phone",
    "private_key",
    "prompt",
    "secret",
    "token",
)


@dataclass(frozen=True)
class RedactionResult:
    value: object
    redacted_fields: tuple[str, ...]
    truncated: bool


def redact(value: object) -> RedactionResult:
    """Return JSON-safe bounded data with sensitive keys replaced."""

    fields: list[str] = []
    bounded, truncated = _redact_value(value, "root", 0, fields)
    return RedactionResult(
        value=bounded,
        redacted_fields=tuple(sorted(set(fields))),
        truncated=truncated,
    )


def canonical_digest(value: object) -> str:
    """Hash only the bounded redacted representation."""

    result = redact(value)
    encoded = json.dumps(
        result.value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _redact_value(
    value: object,
    field_path: str,
    depth: int,
    redacted_fields: list[str],
) -> tuple[object, bool]:
    if depth > MAXIMUM_REDACTION_DEPTH:
        return TRUNCATED_VALUE, True
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) > MAXIMUM_REDACTION_STRING_BYTES:
            return (
                encoded[:MAXIMUM_REDACTION_STRING_BYTES].decode(
                    "utf-8", errors="ignore"
                )
                + TRUNCATED_VALUE,
                True,
            )
        return value, False
    if value is None or isinstance(value, (bool, int)):
        return value, False
    if isinstance(value, float):
        finite = math.isfinite(value)
        return value if finite else TRUNCATED_VALUE, not finite
    if isinstance(value, Mapping):
        bounded: dict[str, object] = {}
        truncated = len(value) > MAXIMUM_REDACTION_ENTRIES
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= MAXIMUM_REDACTION_ENTRIES:
                break
            key = str(raw_key)[:MAXIMUM_REDACTION_STRING_BYTES]
            child_path = f"{field_path}.{key}"
            if _is_sensitive_key(key):
                bounded[key] = REDACTED_VALUE
                redacted_fields.append(child_path)
                continue
            bounded_value, child_truncated = _redact_value(
                raw_value,
                child_path,
                depth + 1,
                redacted_fields,
            )
            bounded[key] = bounded_value
            truncated = truncated or child_truncated
        return bounded, truncated
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        bounded_items: list[object] = []
        truncated = len(value) > MAXIMUM_REDACTION_ITEMS
        for index, item in enumerate(value):
            if index >= MAXIMUM_REDACTION_ITEMS:
                break
            bounded_item, child_truncated = _redact_value(
                item,
                f"{field_path}[{index}]",
                depth + 1,
                redacted_fields,
            )
            bounded_items.append(bounded_item)
            truncated = truncated or child_truncated
        return bounded_items, truncated
    return TRUNCATED_VALUE, True


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)
