"""Strict secret-free wire contracts for OpenAI Responses requests."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.services.harness.protocol import (
    StrictProtocolModel,
    ToolName,
)
from app.services.harness.protocol.provider_stream import ProviderCallId
from app.services.harness.protocol.routing import ModelName


class OpenAIInputText(StrictProtocolModel):
    type: Literal["input_text"] = "input_text"
    text: str = Field(min_length=1, max_length=128 * 1024)


class OpenAIMessageInput(StrictProtocolModel):
    type: Literal["message"] = "message"
    role: Literal["assistant", "developer", "system", "user"]
    content: tuple[OpenAIInputText, ...] = Field(
        min_length=1,
        max_length=64,
    )


class OpenAIFunctionCallOutput(StrictProtocolModel):
    type: Literal["function_call_output"] = "function_call_output"
    call_id: ProviderCallId
    output: str = Field(min_length=1, max_length=128 * 1024)


type OpenAIResponseInput = OpenAIMessageInput | OpenAIFunctionCallOutput


class OpenAIFunctionTool(StrictProtocolModel):
    type: Literal["function"] = "function"
    name: ToolName
    description: str = Field(min_length=1, max_length=4_096)
    parameters: dict[str, object]
    strict: Literal[True] = True


class CompiledOpenAIResponsesRequest(StrictProtocolModel):
    model: ModelName
    input: tuple[OpenAIResponseInput, ...] = Field(
        min_length=1,
        max_length=512,
    )
    tools: tuple[OpenAIFunctionTool, ...] = Field(max_length=256)
    max_output_tokens: int = Field(ge=1, le=512_000)
    stream: Literal[True] = True
    store: Literal[False] = False
    parallel_tool_calls: Literal[True] = True
    truncation: Literal["disabled"] = "disabled"
    prompt_cache_key: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
