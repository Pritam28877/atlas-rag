"""Bounded provider-neutral input compiled only after route selection."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedReason,
    Capability,
    Payload,
    RequestId,
    Sha256,
    StrictProtocolModel,
    TurnId,
    UtcTimestamp,
)
from app.services.harness.protocol.conversation import DataClassification
from app.services.harness.protocol.execution import ToolName, ToolVersion
from app.services.harness.protocol.provider_stream import ProviderCallId
from app.services.harness.protocol.routing import RouteId

MAXIMUM_PROVIDER_REQUEST_BYTES = 16 * 1024 * 1024


class ProviderModality(StrEnum):
    AUDIO = "audio"
    DOCUMENT = "document"
    IMAGE = "image"
    TEXT = "text"
    VIDEO = "video"


class ProviderMessageRole(StrEnum):
    ASSISTANT = "assistant"
    DEVELOPER = "developer"
    SYSTEM = "system"
    TOOL = "tool"
    USER = "user"


class ProviderContentPart(StrictProtocolModel):
    modality: ProviderModality
    payload: Payload


class ProviderMessage(StrictProtocolModel):
    role: ProviderMessageRole
    parts: tuple[ProviderContentPart, ...] = Field(
        min_length=1,
        max_length=64,
    )
    tool_call_id: ProviderCallId | None = None

    @model_validator(mode="after")
    def validate_tool_result(self) -> Self:
        is_tool_result = self.role is ProviderMessageRole.TOOL
        if is_tool_result != (self.tool_call_id is not None):
            raise ValueError("tool messages require exactly one tool call ID")
        return self


class ProviderToolDefinition(StrictProtocolModel):
    name: ToolName
    version: ToolVersion
    description: BoundedReason
    input_schema_json: str = Field(min_length=2, max_length=128 * 1024)
    input_schema_sha256: Sha256

    @model_validator(mode="after")
    def validate_schema(self) -> Self:
        try:
            schema = json.loads(self.input_schema_json)
        except json.JSONDecodeError as error:
            raise ValueError("tool input schema must be valid JSON") from error
        if not isinstance(schema, dict):
            raise ValueError("tool input schema must be a JSON object")
        canonical = json.dumps(
            schema,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if canonical != self.input_schema_json:
            raise ValueError("tool input schema must use canonical JSON")
        digest = hashlib.sha256(self.input_schema_json.encode()).hexdigest()
        if digest != self.input_schema_sha256:
            raise ValueError("tool input schema hash mismatch")
        return self


class CanonicalProviderRequest(StrictProtocolModel):
    request_id: RequestId
    turn_id: TurnId
    route_id: RouteId
    classification: DataClassification
    messages: tuple[ProviderMessage, ...] = Field(
        min_length=1,
        max_length=512,
    )
    tools: tuple[ProviderToolDefinition, ...] = Field(max_length=256)
    required_capabilities: tuple[Capability, ...] = Field(max_length=64)
    output_modalities: tuple[ProviderModality, ...] = Field(
        min_length=1,
        max_length=5,
    )
    reserved_output_tokens: int = Field(ge=1, le=512_000)
    reserved_reasoning_tokens: int = Field(ge=0, le=512_000)
    deadline_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_canonical_request(self) -> Self:
        for values, label in (
            (self.required_capabilities, "required capabilities"),
            (self.output_modalities, "output modalities"),
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{label} must be unique and sorted")
        tool_keys = tuple((tool.name, tool.version) for tool in self.tools)
        if tuple(sorted(set(tool_keys))) != tool_keys:
            raise ValueError("provider tools must be unique and sorted")
        tool_call_ids = tuple(
            message.tool_call_id
            for message in self.messages
            if message.tool_call_id is not None
        )
        if len(set(tool_call_ids)) != len(tool_call_ids):
            raise ValueError("tool result call IDs must be unique")
        total_bytes = sum(
            part.payload.size_bytes
            for message in self.messages
            for part in message.parts
        )
        total_bytes += sum(
            len(tool.input_schema_json.encode()) for tool in self.tools
        )
        if total_bytes > MAXIMUM_PROVIDER_REQUEST_BYTES:
            raise ValueError("canonical provider request exceeds 16 MiB")
        return self
