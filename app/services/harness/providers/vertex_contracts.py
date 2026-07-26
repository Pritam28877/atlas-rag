"""Strict wire contracts for Vertex Gemini streamGenerateContent."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol import StrictProtocolModel
from app.services.harness.protocol.base import Sha256
from app.services.harness.providers.vertex_policy import VertexModelId

VertexFunctionName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$",
    ),
]


class VertexTextPart(StrictProtocolModel):
    text: str = Field(min_length=1, max_length=128 * 1024)

    def to_wire(self) -> dict[str, object]:
        return {"text": self.text}


class VertexContent(StrictProtocolModel):
    role: Literal["model", "user"]
    parts: tuple[VertexTextPart, ...] = Field(min_length=1, max_length=64)

    def to_wire(self) -> dict[str, object]:
        return {
            "role": self.role,
            "parts": [part.to_wire() for part in self.parts],
        }


class VertexSystemInstruction(StrictProtocolModel):
    parts: tuple[VertexTextPart, ...] = Field(min_length=1, max_length=64)

    def to_wire(self) -> dict[str, object]:
        return {"parts": [part.to_wire() for part in self.parts]}


class VertexFunctionDeclaration(StrictProtocolModel):
    name: VertexFunctionName
    description: str = Field(min_length=1, max_length=4_096)
    parameters: dict[str, object]

    def to_wire(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class VertexFunctionTool(StrictProtocolModel):
    function_declarations: tuple[VertexFunctionDeclaration, ...] = Field(
        min_length=1,
        max_length=256,
    )

    def to_wire(self) -> dict[str, object]:
        return {
            "functionDeclarations": [
                declaration.to_wire()
                for declaration in self.function_declarations
            ]
        }


class VertexFunctionCallingConfig(StrictProtocolModel):
    mode: Literal["ANY"] = "ANY"
    allowed_function_names: tuple[VertexFunctionName, ...] = Field(
        min_length=1,
        max_length=256,
    )

    def to_wire(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "allowedFunctionNames": list(self.allowed_function_names),
        }


class VertexToolConfig(StrictProtocolModel):
    function_calling_config: VertexFunctionCallingConfig

    def to_wire(self) -> dict[str, object]:
        return {
            "functionCallingConfig": self.function_calling_config.to_wire()
        }


class VertexThinkingConfig(StrictProtocolModel):
    include_thoughts: Literal[True] = True
    thinking_budget: int = Field(ge=1, le=512_000)

    def to_wire(self) -> dict[str, object]:
        return {
            "includeThoughts": self.include_thoughts,
            "thinkingBudget": self.thinking_budget,
        }


class VertexGenerationConfig(StrictProtocolModel):
    max_output_tokens: int = Field(ge=1, le=512_000)
    thinking_config: VertexThinkingConfig | None = None

    def to_wire(self) -> dict[str, object]:
        value: dict[str, object] = {
            "maxOutputTokens": self.max_output_tokens
        }
        if self.thinking_config is not None:
            value["thinkingConfig"] = self.thinking_config.to_wire()
        return value


class CompiledVertexGenerateContentRequest(StrictProtocolModel):
    model: VertexModelId
    contents: tuple[VertexContent, ...] = Field(min_length=1, max_length=512)
    system_instruction: VertexSystemInstruction | None = None
    tools: tuple[VertexFunctionTool, ...] = Field(max_length=1)
    tool_config: VertexToolConfig | None = None
    generation_config: VertexGenerationConfig

    @model_validator(mode="after")
    def validate_tool_config(self) -> Self:
        if self.tool_config is not None and not self.tools:
            raise ValueError("Vertex tool config requires declared tools")
        return self

    def to_wire(self) -> dict[str, object]:
        request: dict[str, object] = {
            "contents": [content.to_wire() for content in self.contents],
            "generationConfig": self.generation_config.to_wire(),
        }
        if self.system_instruction is not None:
            request["systemInstruction"] = self.system_instruction.to_wire()
        if self.tools:
            request["tools"] = [tool.to_wire() for tool in self.tools]
        if self.tool_config is not None:
            request["toolConfig"] = self.tool_config.to_wire()
        return request


class VertexStreamMetadata(StrictProtocolModel):
    finish_reason: str = Field(min_length=1, max_length=128)
    response_id_sha256: Sha256 | None = None
    model_version_sha256: Sha256 | None = None
    thought_signature_sha256: Sha256 | None = None
    safety_metadata_sha256: Sha256 | None = None
    total_tokens: int = Field(ge=0, le=2_000_000)
    tool_use_prompt_tokens: int = Field(ge=0, le=2_000_000)
