"""Fail-closed compiler from canonical input to Bedrock ConverseStream."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal, cast

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
from app.services.harness.providers.bedrock_contracts import (
    BedrockInferenceConfiguration,
    BedrockMessage,
    BedrockMessageContent,
    BedrockSystemContent,
    BedrockTextContent,
    BedrockToolResultContent,
    BedrockToolSpecification,
    CompiledBedrockConverseStreamRequest,
)

type BedrockMessageRole = Literal["assistant", "user"]


class BedrockCompileErrorCode(StrEnum):
    CONTEXT = "context"
    MESSAGE = "message"
    MODEL = "model"
    MODALITY = "modality"
    PAYLOAD = "payload"
    TOOL_NAME = "tool_name"
    TOOL_RESULT = "tool_result"
    TOOL_SCHEMA = "tool_schema"


class BedrockCompileError(ValueError):
    def __init__(self, code: BedrockCompileErrorCode) -> None:
        super().__init__("Bedrock ConverseStream request compilation failed")
        self.code = code


class BedrockConverseStreamCompiler:
    def compile(
        self,
        request: CanonicalProviderRequest,
        model: ProviderModelCapabilities,
        context: ProviderContextPlan,
    ) -> CompiledBedrockConverseStreamRequest:
        _validate_evidence(request, model, context)
        system: list[BedrockSystemContent] = []
        messages: list[BedrockMessage] = []
        conversation_started = False
        for message in request.messages:
            if message.role in {
                ProviderMessageRole.SYSTEM,
                ProviderMessageRole.DEVELOPER,
            }:
                if conversation_started:
                    raise BedrockCompileError(BedrockCompileErrorCode.MESSAGE)
                system.extend(_system_content(message))
                continue
            conversation_started = True
            messages.append(_message(message))
        if not messages:
            raise BedrockCompileError(BedrockCompileErrorCode.MESSAGE)
        tools = tuple(_tool(tool) for tool in request.tools)
        return CompiledBedrockConverseStreamRequest(
            model_id=model.model,
            messages=tuple(messages),
            system=tuple(system),
            inference_config=BedrockInferenceConfiguration(
                max_tokens=(
                    request.reserved_output_tokens + request.reserved_reasoning_tokens
                )
            ),
            tools=tools,
        )


def _validate_evidence(
    request: CanonicalProviderRequest,
    model: ProviderModelCapabilities,
    context: ProviderContextPlan,
) -> None:
    request_sha256 = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    output_tokens = request.reserved_output_tokens + request.reserved_reasoning_tokens
    if (
        model.provider != "bedrock"
        or context.model_revision_sha256 != model.model_revision_sha256
        or output_tokens > model.max_output_tokens
    ):
        raise BedrockCompileError(BedrockCompileErrorCode.MODEL)
    if context.provider_request_sha256 != request_sha256:
        raise BedrockCompileError(BedrockCompileErrorCode.CONTEXT)
    if context.applied_features:
        raise BedrockCompileError(BedrockCompileErrorCode.CONTEXT)
    if (
        request.output_modalities != (ProviderModality.TEXT,)
        or ProviderModality.TEXT not in model.output_modalities
    ):
        raise BedrockCompileError(BedrockCompileErrorCode.MODALITY)


def _system_content(
    message: ProviderMessage,
) -> tuple[BedrockSystemContent, ...]:
    content: list[BedrockSystemContent] = []
    for part in message.parts:
        content.append(BedrockSystemContent(text=_inline_text(part)))
    return tuple(content)


def _message(message: ProviderMessage) -> BedrockMessage:
    if message.role is ProviderMessageRole.TOOL:
        if len(message.parts) != 1 or message.tool_call_id is None:
            raise BedrockCompileError(BedrockCompileErrorCode.TOOL_RESULT)
        content: tuple[BedrockMessageContent, ...] = (
            BedrockToolResultContent(
                tool_use_id=message.tool_call_id,
                text=_inline_text(message.parts[0]),
            ),
        )
        return BedrockMessage(role="user", content=content)
    if message.role not in {
        ProviderMessageRole.ASSISTANT,
        ProviderMessageRole.USER,
    }:
        raise BedrockCompileError(BedrockCompileErrorCode.MESSAGE)
    role = cast(BedrockMessageRole, message.role.value)
    text_content = tuple(
        BedrockTextContent(text=_inline_text(part)) for part in message.parts
    )
    return BedrockMessage(role=role, content=text_content)


def _inline_text(part: ProviderContentPart) -> str:
    if part.modality is not ProviderModality.TEXT:
        raise BedrockCompileError(BedrockCompileErrorCode.MODALITY)
    if isinstance(part.payload, BlobPayload):
        raise BedrockCompileError(BedrockCompileErrorCode.PAYLOAD)
    if not isinstance(part.payload, InlinePayload):
        raise BedrockCompileError(BedrockCompileErrorCode.PAYLOAD)
    return part.payload.text


def _tool(tool: ProviderToolDefinition) -> BedrockToolSpecification:
    name = tool.name
    if len(name) > 64 or not name.replace("_", "").replace("-", "").isalnum():
        raise BedrockCompileError(BedrockCompileErrorCode.TOOL_NAME)
    try:
        schema = json.loads(tool.input_schema_json)
    except json.JSONDecodeError:
        raise BedrockCompileError(BedrockCompileErrorCode.TOOL_SCHEMA) from None
    if not isinstance(schema, dict):
        raise BedrockCompileError(BedrockCompileErrorCode.TOOL_SCHEMA)
    return BedrockToolSpecification(
        name=name,
        description=tool.description,
        input_schema=schema,
    )
