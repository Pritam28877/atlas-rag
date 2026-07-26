"""Fail-closed compiler from canonical input to Vertex Gemini."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from app.services.harness.protocol import (
    BlobPayload,
    CanonicalProviderRequest,
    InlinePayload,
    ProviderContentPart,
    ProviderContextPlan,
    ProviderMessage,
    ProviderMessageRole,
    ProviderModality,
    ProviderModelCapabilities,
    ProviderToolDefinition,
)
from app.services.harness.providers.vertex_contracts import (
    CompiledVertexGenerateContentRequest,
    VertexContent,
    VertexFunctionCallingConfig,
    VertexFunctionDeclaration,
    VertexFunctionTool,
    VertexGenerationConfig,
    VertexSystemInstruction,
    VertexTextPart,
    VertexThinkingConfig,
    VertexToolConfig,
)

type VertexContentRole = Literal["model", "user"]


class VertexCompileErrorCode(StrEnum):
    CONTEXT = "context"
    MESSAGE = "message"
    MODEL = "model"
    MODALITY = "modality"
    PAYLOAD = "payload"
    TOOL_HISTORY = "tool_history"
    TOOL_NAME = "tool_name"
    TOOL_SCHEMA = "tool_schema"


class VertexCompileError(ValueError):
    def __init__(self, code: VertexCompileErrorCode) -> None:
        super().__init__("Vertex GenerateContent request compilation failed")
        self.code = code


class VertexGenerateContentCompiler:
    def compile(
        self,
        request: CanonicalProviderRequest,
        model: ProviderModelCapabilities,
        context: ProviderContextPlan,
        *,
        tool_choice: str | None = None,
    ) -> CompiledVertexGenerateContentRequest:
        _validate_evidence(request, model, context)
        system_parts: list[VertexTextPart] = []
        contents: list[VertexContent] = []
        conversation_started = False
        for message in request.messages:
            if message.role in {
                ProviderMessageRole.SYSTEM,
                ProviderMessageRole.DEVELOPER,
            }:
                if conversation_started:
                    raise VertexCompileError(VertexCompileErrorCode.MESSAGE)
                system_parts.extend(_text_parts(message))
                continue
            conversation_started = True
            contents.append(_content(message))
        if not contents:
            raise VertexCompileError(VertexCompileErrorCode.MESSAGE)
        declarations = tuple(_tool(tool) for tool in request.tools)
        tool_config = _tool_config(declarations, tool_choice)
        reasoning_tokens = request.reserved_reasoning_tokens
        thinking_config = (
            VertexThinkingConfig(thinking_budget=reasoning_tokens)
            if reasoning_tokens > 0
            else None
        )
        tools = (
            (VertexFunctionTool(function_declarations=declarations),)
            if declarations
            else ()
        )
        return CompiledVertexGenerateContentRequest(
            model=model.model,
            contents=tuple(contents),
            system_instruction=(
                VertexSystemInstruction(parts=tuple(system_parts))
                if system_parts
                else None
            ),
            tools=tools,
            tool_config=tool_config,
            generation_config=VertexGenerationConfig(
                max_output_tokens=(
                    request.reserved_output_tokens + reasoning_tokens
                ),
                thinking_config=thinking_config,
            ),
        )


def _validate_evidence(
    request: CanonicalProviderRequest,
    model: ProviderModelCapabilities,
    context: ProviderContextPlan,
) -> None:
    output_tokens = request.reserved_output_tokens + request.reserved_reasoning_tokens
    request_sha256 = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    if (
        model.provider != "vertex"
        or context.model_revision_sha256 != model.model_revision_sha256
        or output_tokens > model.max_output_tokens
    ):
        raise VertexCompileError(VertexCompileErrorCode.MODEL)
    if (
        context.provider_request_sha256 != request_sha256
        or context.applied_features
    ):
        raise VertexCompileError(VertexCompileErrorCode.CONTEXT)
    if (
        request.output_modalities != (ProviderModality.TEXT,)
        or ProviderModality.TEXT not in model.output_modalities
    ):
        raise VertexCompileError(VertexCompileErrorCode.MODALITY)


def _content(message: ProviderMessage) -> VertexContent:
    if message.role is ProviderMessageRole.TOOL:
        raise VertexCompileError(VertexCompileErrorCode.TOOL_HISTORY)
    if message.role not in {
        ProviderMessageRole.ASSISTANT,
        ProviderMessageRole.USER,
    }:
        raise VertexCompileError(VertexCompileErrorCode.MESSAGE)
    role: VertexContentRole = (
        "model" if message.role is ProviderMessageRole.ASSISTANT else "user"
    )
    return VertexContent(role=role, parts=_text_parts(message))


def _text_parts(message: ProviderMessage) -> tuple[VertexTextPart, ...]:
    return tuple(VertexTextPart(text=_inline_text(part)) for part in message.parts)


def _inline_text(part: ProviderContentPart) -> str:
    if part.modality is not ProviderModality.TEXT:
        raise VertexCompileError(VertexCompileErrorCode.MODALITY)
    if isinstance(part.payload, BlobPayload) or not isinstance(
        part.payload,
        InlinePayload,
    ):
        raise VertexCompileError(VertexCompileErrorCode.PAYLOAD)
    return part.payload.text


def _tool(tool: ProviderToolDefinition) -> VertexFunctionDeclaration:
    name = tool.name
    valid_name = (
        len(name) <= 64
        and (name[0].isalpha() or name[0] == "_")
        and all(character.isalnum() or character in "_.-" for character in name)
    )
    if not valid_name or not name.isascii():
        raise VertexCompileError(VertexCompileErrorCode.TOOL_NAME)
    try:
        schema = json.loads(tool.input_schema_json)
    except json.JSONDecodeError:
        raise VertexCompileError(VertexCompileErrorCode.TOOL_SCHEMA) from None
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise VertexCompileError(VertexCompileErrorCode.TOOL_SCHEMA)
    return VertexFunctionDeclaration(
        name=name,
        description=tool.description,
        parameters=schema,
    )


def _tool_config(
    declarations: tuple[VertexFunctionDeclaration, ...],
    tool_choice: str | None,
) -> VertexToolConfig | None:
    if tool_choice is None:
        return None
    if tool_choice not in {declaration.name for declaration in declarations}:
        raise VertexCompileError(VertexCompileErrorCode.TOOL_NAME)
    return VertexToolConfig(
        function_calling_config=VertexFunctionCallingConfig(
            allowed_function_names=(tool_choice,)
        )
    )
