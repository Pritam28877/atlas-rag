"""Bounded projection and opaque-preservation rules for protocol rollback."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Annotated, Self, cast

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol.base import (
    SchemaVersion,
    Sha256,
    StrictProtocolModel,
)

CURRENT_SCHEMA_VERSION: SchemaVersion = "1.2"
READABLE_SCHEMA_VERSIONS: tuple[SchemaVersion, ...] = ("1.0", "1.1", "1.2")
MAX_COMPATIBLE_MINOR_DISTANCE = 2
MAX_OPAQUE_RECORD_BYTES = 128 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 10_000
_SCHEMA_VERSION_PATTERN = re.compile(r"^1\.([0-9]{1,3})$")
_RECORD_TYPE_PATTERN = re.compile(
    r"^[A-Za-z][A-Za-z0-9]*(?:[._:-][A-Za-z0-9]+)*$"
)

type CompatibleRecordType = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9]*(?:[._:-][A-Za-z0-9]+)*$",
    ),
]


class CompatibilityError(ValueError):
    """Raised when a record is unsafe or outside the compatibility window."""


class CompatibilityAction(StrEnum):
    PROJECT = "project"
    PRESERVE_OPAQUE = "preserve_opaque"
    REJECT = "reject"


class CompatibleRecordKind(StrEnum):
    COMMAND = "command"
    EVENT = "event"


def _minor_version(schema_version: str) -> int:
    if not isinstance(schema_version, str):
        raise CompatibilityError("schema version must be a string")
    match = _SCHEMA_VERSION_PATTERN.fullmatch(schema_version)
    if match is None:
        raise CompatibilityError(f"unsupported schema version: {schema_version!r}")
    return int(match.group(1))


def compatibility_action(
    record_schema_version: str,
    reader_schema_version: str,
) -> CompatibilityAction:
    current_minor = _minor_version(CURRENT_SCHEMA_VERSION)
    record_minor = _minor_version(record_schema_version)
    reader_minor = _minor_version(reader_schema_version)
    if record_minor > current_minor or reader_minor > current_minor:
        return CompatibilityAction.REJECT
    distance = abs(reader_minor - record_minor)
    if distance > MAX_COMPATIBLE_MINOR_DISTANCE:
        return CompatibilityAction.REJECT
    if record_minor <= reader_minor:
        return CompatibilityAction.PROJECT
    return CompatibilityAction.PRESERVE_OPAQUE


def require_writer_rollback_compatibility(
    writer_schema_version: str,
    rollback_reader_versions: tuple[str, ...],
) -> tuple[CompatibilityAction, ...]:
    reader_minors = tuple(
        _minor_version(reader_version)
        for reader_version in rollback_reader_versions
    )
    if tuple(sorted(set(reader_minors))) != reader_minors:
        raise CompatibilityError(
            "rollback reader versions must be unique and sorted"
        )
    actions = tuple(
        compatibility_action(writer_schema_version, reader_version)
        for reader_version in rollback_reader_versions
    )
    if not actions or CompatibilityAction.REJECT in actions:
        raise CompatibilityError("writer is unsafe for declared rollback readers")
    return actions


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    record: dict[str, object] = {}
    for key, value in pairs:
        if key in record:
            raise CompatibilityError(f"duplicate JSON key: {key}")
        record[key] = value
    return record


def _reject_non_json_constant(value: str) -> None:
    raise CompatibilityError(f"non-JSON numeric constant: {value}")


def _parse_json_object(raw_json: bytes) -> dict[str, object]:
    if not 2 <= len(raw_json) <= MAX_OPAQUE_RECORD_BYTES:
        raise CompatibilityError("opaque record size is outside its bound")
    try:
        text = raw_json.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
    ) as error:
        raise CompatibilityError(f"opaque record is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise CompatibilityError("opaque record must be a JSON object")
    record = cast(dict[str, object], value)
    _validate_json_bounds(record)
    return record


def _validate_json_bounds(root: object) -> None:
    pending: list[tuple[object, int]] = [(root, 1)]
    node_count = 0
    while pending:
        value, depth = pending.pop()
        node_count += 1
        if node_count > MAX_JSON_NODES:
            raise CompatibilityError("opaque record exceeds JSON node bound")
        if depth > MAX_JSON_DEPTH:
            raise CompatibilityError("opaque record exceeds JSON depth bound")
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)


def _record_identity(
    record: dict[str, object],
) -> tuple[SchemaVersion, CompatibleRecordKind, CompatibleRecordType]:
    schema_version = record.get("schema_version")
    if not isinstance(schema_version, str):
        raise CompatibilityError("opaque record lacks schema_version")
    _minor_version(schema_version)
    command = record.get("command")
    if isinstance(command, dict):
        record_kind = CompatibleRecordKind.COMMAND
        record_type = command.get("kind")
    else:
        record_kind = CompatibleRecordKind.EVENT
        record_type = record.get("event_type")
    if not isinstance(record_type, str):
        raise CompatibilityError("opaque record lacks command kind or event_type")
    if (
        len(record_type) > 128
        or _RECORD_TYPE_PATTERN.fullmatch(record_type) is None
    ):
        raise CompatibilityError("opaque record type is invalid")
    return schema_version, record_kind, record_type


class PreservedUnknownRecord(StrictProtocolModel):
    """Exact bounded bytes retained when an older reader cannot project a record."""

    schema_version: SchemaVersion
    reader_schema_version: SchemaVersion
    reader_action: CompatibilityAction
    record_kind: CompatibleRecordKind
    record_type: CompatibleRecordType
    raw_json: bytes = Field(min_length=2, max_length=MAX_OPAQUE_RECORD_BYTES)
    size_bytes: int = Field(ge=2, le=MAX_OPAQUE_RECORD_BYTES)
    content_sha256: Sha256

    @model_validator(mode="after")
    def validate_exact_record(self) -> Self:
        if len(self.raw_json) != self.size_bytes:
            raise ValueError("opaque record size does not match exact bytes")
        actual_digest = hashlib.sha256(self.raw_json).hexdigest()
        if actual_digest != self.content_sha256:
            raise ValueError("opaque record hash does not match exact bytes")
        record = _parse_json_object(self.raw_json)
        schema_version, record_kind, record_type = _record_identity(record)
        if (
            schema_version != self.schema_version
            or record_kind is not self.record_kind
            or record_type != self.record_type
        ):
            raise ValueError("opaque record identity does not match exact bytes")
        expected_action = compatibility_action(
            self.schema_version,
            self.reader_schema_version,
        )
        if expected_action is not self.reader_action:
            raise ValueError("opaque record compatibility action is inconsistent")
        if self.reader_action is CompatibilityAction.REJECT:
            raise ValueError("rejected record cannot be preserved")
        return self


def preserve_unknown_record(
    raw_json: bytes,
    reader_schema_version: SchemaVersion,
) -> PreservedUnknownRecord:
    record = _parse_json_object(raw_json)
    schema_version, record_kind, record_type = _record_identity(record)
    action = compatibility_action(schema_version, reader_schema_version)
    if action is CompatibilityAction.REJECT:
        raise CompatibilityError("record is outside the reader compatibility window")
    return PreservedUnknownRecord(
        schema_version=schema_version,
        reader_schema_version=reader_schema_version,
        reader_action=action,
        record_kind=record_kind,
        record_type=record_type,
        raw_json=raw_json,
        size_bytes=len(raw_json),
        content_sha256=hashlib.sha256(raw_json).hexdigest(),
    )
