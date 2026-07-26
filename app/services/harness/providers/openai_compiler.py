"""Fail-closed compiler from canonical input to OpenAI Responses."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal, cast

from app.services.harness.protocol import (
    BlobPayload,
    CanonicalProviderRequest,
    InlinePayload,
    ProviderContextFeature,
    ProviderContextPlan,
    ProviderMessage,
    ProviderMessageRole,
    ProviderModality,
    ProviderModelCapabilities,
)
from app.services.harness.providers.openai_contracts import (
    CompiledOpenAIResponsesRequest,
    OpenAIFunctionCallOutput,
    OpenAIFunctionTool,
    OpenAIInputText,
    OpenAIMessageInput,
    OpenAIResponseInput,
)

type OpenAIMessageRole = Literal["assistant", "developer", "system", "user"]


class OpenAICompileErrorCode(StrEnum):
    CONTEXT = "context"
    MODEL = "model"
    MODALITY = "modality"
    PAYLOAD = "payload"
    TOOL_RESULT = "tool_result"
    TOOL_SCHEMA = "tool_schema"


class OpenAICompileError(ValueError):
    def __init__(self, code: OpenAICompileErrorCode) -> None:
        super().__init__("OpenAI Responses request compilation failed")
        self.code = code


class OpenAIResponsesCompiler:
    def __init__(self, *, provider: str = "openai") -> None:
        self._provider = provider

    def compile(
        self,
        request: CanonicalProviderRequest,
        model: ProviderModelCapabilities,
        context: ProviderContextPlan,
    ) -> CompiledOpenAIResponsesRequest:
        self._validate_evidence(request, model, context)
        output_tokens = (
            request.reserved_output_tokens
            + request.reserved_reasoning_tokens
        )
        if output_tokens > model.max_output_tokens:
            raise OpenAICompileError(OpenAICompileErrorCode.MODEL)
        inputs = tuple(_compile_message(message) for message in request.messages)
        tools = tuple(
            OpenAIFunctionTool(
                name=tool.name,
                description=tool.description,
                parameters=_strict_schema(tool.input_schema_json),
            )
            for tool in request.tools
        )
        return CompiledOpenAIResponsesRequest(
            model=model.model,
            input=inputs,
            tools=tools,
            max_output_tokens=output_tokens,
            prompt_cache_key=context.prompt_cache_key_sha256,
        )

    def _validate_evidence(
        self,
        request: CanonicalProviderRequest,
        model: ProviderModelCapabilities,
        context: ProviderContextPlan,
    ) -> None:
        request_sha256 = hashlib.sha256(
            request.model_dump_json().encode()
        ).hexdigest()
        if (
            model.provider != self._provider
            or context.model_revision_sha256 != model.model_revision_sha256
        ):
            raise OpenAICompileError(OpenAICompileErrorCode.MODEL)
        if context.provider_request_sha256 != request_sha256:
            raise OpenAICompileError(OpenAICompileErrorCode.CONTEXT)
        unsupported_context = set(context.applied_features) - {
            ProviderContextFeature.PROMPT_CACHE
        }
        if unsupported_context:
            raise OpenAICompileError(OpenAICompileErrorCode.CONTEXT)
        if request.output_modalities != (ProviderModality.TEXT,):
            raise OpenAICompileError(OpenAICompileErrorCode.MODALITY)
        if ProviderModality.TEXT not in model.output_modalities:
            raise OpenAICompileError(OpenAICompileErrorCode.MODEL)


def _compile_message(message: ProviderMessage) -> OpenAIResponseInput:
    if message.role is ProviderMessageRole.TOOL:
        return _compile_tool_result(message)
    content: list[OpenAIInputText] = []
    for part in message.parts:
        if part.modality is not ProviderModality.TEXT:
            raise OpenAICompileError(OpenAICompileErrorCode.MODALITY)
        if isinstance(part.payload, BlobPayload):
            raise OpenAICompileError(OpenAICompileErrorCode.PAYLOAD)
        content.append(OpenAIInputText(text=part.payload.text))
    role = cast(OpenAIMessageRole, message.role.value)
    return OpenAIMessageInput(role=role, content=tuple(content))


def _compile_tool_result(
    message: ProviderMessage,
) -> OpenAIFunctionCallOutput:
    if len(message.parts) != 1 or message.tool_call_id is None:
        raise OpenAICompileError(OpenAICompileErrorCode.TOOL_RESULT)
    part = message.parts[0]
    if (
        part.modality is not ProviderModality.TEXT
        or not isinstance(part.payload, InlinePayload)
    ):
        raise OpenAICompileError(OpenAICompileErrorCode.TOOL_RESULT)
    return OpenAIFunctionCallOutput(
        call_id=message.tool_call_id,
        output=part.payload.text,
    )


def _strict_schema(schema_json: str) -> dict[str, object]:
    schema = cast(dict[str, object], json.loads(schema_json))
    pending: list[object] = [schema]
    visited = 0
    while pending:
        current = pending.pop()
        visited += 1
        if visited > 4_096:
            raise OpenAICompileError(OpenAICompileErrorCode.TOOL_SCHEMA)
        if isinstance(current, list):
            pending.extend(current)
            continue
        if not isinstance(current, dict):
            continue
        if current.get("type") == "object":
            properties = current.get("properties")
            required = current.get("required")
            required_names = (
                required
                if (
                    isinstance(required, list)
                    and all(isinstance(name, str) for name in required)
                )
                else None
            )
            if (
                not isinstance(properties, dict)
                or current.get("additionalProperties") is not False
                or required_names is None
                or len(required_names) != len(properties)
                or set(required_names) != set(properties)
            ):
                raise OpenAICompileError(
                    OpenAICompileErrorCode.TOOL_SCHEMA
                )
        pending.extend(current.values())
    return schema
