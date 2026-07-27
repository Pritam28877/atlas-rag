"""Immutable tool identity, schema, policy, and output contracts."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    Capability,
    MediaType,
    Sha256,
    StrictProtocolModel,
)
from app.services.harness.protocol.execution import (
    IdempotencyClass,
    ToolName,
    ToolVersion,
)
from app.services.harness.protocol.operation_admission import (
    canonical_operation_args_sha256,
)
from app.services.harness.protocol.provider_stream import ProviderCallId

MAXIMUM_TOOL_ALIASES = 16
MAXIMUM_TOOL_SCHEMA_BYTES = 64 * 1024
DEFAULT_TOOL_OUTPUT_BYTES = 1024 * 1024
MAXIMUM_TOOL_OUTPUT_BYTES = 16 * 1024 * 1024


class ToolOutputOverflow(StrEnum):
    ARTIFACT = "artifact"
    REJECT = "reject"
    TRUNCATE = "truncate"


class ToolOutputContract(StrictProtocolModel):
    media_type: MediaType = "text/plain"
    maximum_bytes: int = Field(
        default=DEFAULT_TOOL_OUTPUT_BYTES,
        ge=1,
        le=MAXIMUM_TOOL_OUTPUT_BYTES,
    )
    maximum_inline_bytes: int = Field(
        default=DEFAULT_TOOL_OUTPUT_BYTES,
        ge=1,
        le=DEFAULT_TOOL_OUTPUT_BYTES,
    )
    overflow: ToolOutputOverflow
    redaction_required: Literal[True] = True
    background_allowed: bool = False

    @model_validator(mode="after")
    def validate_inline_limit(self) -> Self:
        if self.maximum_inline_bytes > self.maximum_bytes:
            raise ValueError("tool inline output exceeds total output")
        return self


class StrictToolArguments(StrictProtocolModel):
    """Base class required for all registered tool argument models."""


class ToolDescriptor(StrictProtocolModel):
    name: ToolName
    version: ToolVersion
    aliases: tuple[ToolName, ...] = Field(
        default=(),
        max_length=MAXIMUM_TOOL_ALIASES,
    )
    default_version: bool
    capability: Capability
    idempotency_class: IdempotencyClass
    input_schema_json: str = Field(
        min_length=2,
        max_length=MAXIMUM_TOOL_SCHEMA_BYTES,
        repr=False,
    )
    input_schema_sha256: Sha256
    output: ToolOutputContract
    descriptor_sha256: Sha256

    @model_validator(mode="after")
    def validate_descriptor(self) -> Self:
        if (
            tuple(sorted(set(self.aliases))) != self.aliases
            or self.name in self.aliases
        ):
            raise ValueError("tool aliases must be unique, sorted, and distinct")
        schema = _canonical_schema_text(self.input_schema_json)
        if schema != self.input_schema_json:
            raise ValueError("tool input schema must use canonical JSON")
        schema_sha256 = hashlib.sha256(schema.encode()).hexdigest()
        if schema_sha256 != self.input_schema_sha256:
            raise ValueError("tool input schema hash is invalid")
        expected = tool_descriptor_sha256(
            name=self.name,
            version=self.version,
            aliases=self.aliases,
            default_version=self.default_version,
            capability=self.capability,
            idempotency_class=self.idempotency_class,
            input_schema_sha256=self.input_schema_sha256,
            output=self.output,
        )
        if self.descriptor_sha256 != expected:
            raise ValueError("tool descriptor hash is invalid")
        return self


class ValidatedToolCall(StrictProtocolModel):
    call_id: ProviderCallId
    requested_name: ToolName
    tool_name: ToolName
    tool_version: ToolVersion
    descriptor_sha256: Sha256
    arguments_json: str = Field(
        min_length=2,
        max_length=MAXIMUM_TOOL_SCHEMA_BYTES,
        repr=False,
    )
    args_sha256: Sha256
    capability: Capability
    idempotency_class: IdempotencyClass
    output: ToolOutputContract

    @model_validator(mode="after")
    def validate_arguments(self) -> Self:
        try:
            arguments = json.loads(self.arguments_json)
            canonical = _canonical_json(arguments, ensure_ascii=False)
            args_sha256 = canonical_operation_args_sha256(arguments)
        except (RecursionError, TypeError, ValueError) as error:
            raise ValueError("validated tool arguments are invalid") from error
        if (
            not isinstance(arguments, dict)
            or canonical != self.arguments_json
            or args_sha256 != self.args_sha256
        ):
            raise ValueError("validated tool arguments are inconsistent")
        return self


def build_tool_descriptor(
    *,
    name: str,
    version: str,
    aliases: tuple[str, ...],
    default_version: bool,
    capability: str,
    idempotency_class: IdempotencyClass,
    argument_model: type[StrictToolArguments],
    output: ToolOutputContract,
) -> ToolDescriptor:
    schema_json = canonical_tool_schema(argument_model)
    schema_sha256 = hashlib.sha256(schema_json.encode()).hexdigest()
    descriptor_sha256 = tool_descriptor_sha256(
        name=name,
        version=version,
        aliases=aliases,
        default_version=default_version,
        capability=capability,
        idempotency_class=idempotency_class,
        input_schema_sha256=schema_sha256,
        output=output,
    )
    return ToolDescriptor(
        name=name,
        version=version,
        aliases=aliases,
        default_version=default_version,
        capability=capability,
        idempotency_class=idempotency_class,
        input_schema_json=schema_json,
        input_schema_sha256=schema_sha256,
        output=output,
        descriptor_sha256=descriptor_sha256,
    )


def canonical_tool_schema(
    argument_model: type[StrictToolArguments],
) -> str:
    if (
        not isinstance(argument_model, type)
        or not issubclass(argument_model, StrictToolArguments)
        or argument_model is StrictToolArguments
    ):
        raise TypeError("tool argument model must be a strict concrete model")
    schema = argument_model.model_json_schema(mode="validation")
    if (
        schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
    ):
        raise ValueError("tool argument schema must forbid unknown fields")
    encoded = _canonical_json(schema, ensure_ascii=True)
    if len(encoded.encode()) > MAXIMUM_TOOL_SCHEMA_BYTES:
        raise ValueError("tool argument schema exceeds 64 KiB")
    return encoded


def tool_descriptor_sha256(
    *,
    name: str,
    version: str,
    aliases: tuple[str, ...],
    default_version: bool,
    capability: str,
    idempotency_class: IdempotencyClass,
    input_schema_sha256: str,
    output: ToolOutputContract,
) -> str:
    values = {
        "name": name,
        "version": version,
        "aliases": aliases,
        "default_version": default_version,
        "capability": capability,
        "idempotency_class": idempotency_class.value,
        "input_schema_sha256": input_schema_sha256,
        "output": output.model_dump(mode="json"),
    }
    return hashlib.sha256(
        _canonical_json(values, ensure_ascii=True).encode()
    ).hexdigest()


def _canonical_schema_text(value: str) -> str:
    try:
        schema = json.loads(value)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("tool input schema is invalid") from error
    if not isinstance(schema, dict):
        raise ValueError("tool input schema must be an object")
    return _canonical_json(schema, ensure_ascii=True)


def _canonical_json(value: object, *, ensure_ascii: bool) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=ensure_ascii,
        separators=(",", ":"),
        sort_keys=True,
    )
