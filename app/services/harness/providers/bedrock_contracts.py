"""Strict secret-free contracts for Bedrock ConverseStream requests."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol import StrictProtocolModel
from app.services.harness.protocol.provider_stream import ProviderCallId
from app.services.harness.protocol.routing import ModelName

BedrockToolName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]


class BedrockTextContent(StrictProtocolModel):
    text: str = Field(min_length=1, max_length=128 * 1024)

    def to_wire(self) -> dict[str, object]:
        return {"text": self.text}


class BedrockToolResultContent(StrictProtocolModel):
    tool_use_id: ProviderCallId
    text: str = Field(min_length=1, max_length=128 * 1024)

    def to_wire(self) -> dict[str, object]:
        return {
            "toolResult": {
                "toolUseId": self.tool_use_id,
                "content": [{"text": self.text}],
            }
        }


type BedrockMessageContent = BedrockTextContent | BedrockToolResultContent


class BedrockMessage(StrictProtocolModel):
    role: Literal["assistant", "user"]
    content: tuple[BedrockMessageContent, ...] = Field(
        min_length=1,
        max_length=64,
    )

    def to_wire(self) -> dict[str, object]:
        return {
            "role": self.role,
            "content": [block.to_wire() for block in self.content],
        }


class BedrockSystemContent(StrictProtocolModel):
    text: str = Field(min_length=1, max_length=128 * 1024)

    def to_wire(self) -> dict[str, object]:
        return {"text": self.text}


class BedrockToolSpecification(StrictProtocolModel):
    name: BedrockToolName
    description: str = Field(min_length=1, max_length=4_096)
    input_schema: dict[str, object]
    strict: Literal[True] = True

    def to_wire(self) -> dict[str, object]:
        return {
            "toolSpec": {
                "name": self.name,
                "description": self.description,
                "inputSchema": {"json": self.input_schema},
                "strict": self.strict,
            }
        }


class BedrockInferenceConfiguration(StrictProtocolModel):
    max_tokens: int = Field(ge=1, le=512_000)

    def to_wire(self) -> dict[str, object]:
        return {"maxTokens": self.max_tokens}


class CompiledBedrockConverseStreamRequest(StrictProtocolModel):
    model_id: ModelName
    messages: tuple[BedrockMessage, ...] = Field(
        min_length=1,
        max_length=512,
    )
    system: tuple[BedrockSystemContent, ...] = Field(max_length=64)
    inference_config: BedrockInferenceConfiguration
    tools: tuple[BedrockToolSpecification, ...] = Field(max_length=256)
    tool_choice: BedrockToolName | None = None

    @model_validator(mode="after")
    def validate_tool_choice(self) -> Self:
        if self.tool_choice is not None and self.tool_choice not in {
            tool.name for tool in self.tools
        }:
            raise ValueError("Bedrock tool choice requires a declared tool")
        return self

    def to_boto_request(self) -> dict[str, object]:
        request: dict[str, object] = {
            "modelId": self.model_id,
            "messages": [message.to_wire() for message in self.messages],
            "inferenceConfig": self.inference_config.to_wire(),
        }
        if self.system:
            request["system"] = [block.to_wire() for block in self.system]
        if self.tools:
            tool_config: dict[str, object] = {
                "tools": [tool.to_wire() for tool in self.tools]
            }
            if self.tool_choice is not None:
                tool_config["toolChoice"] = {
                    "tool": {"name": self.tool_choice}
                }
            request["toolConfig"] = tool_config
        return request


class BedrockStreamMetadata(StrictProtocolModel):
    stop_reason: str | None = Field(default=None, max_length=128)
    latency_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    cache_write_input_tokens: int = Field(ge=0, le=2_000_000)
    reasoning_signature_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    provider_metadata_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
