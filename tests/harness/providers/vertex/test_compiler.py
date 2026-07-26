import hashlib

import pytest

from app.services.harness.protocol import (
    ProviderContextPlan,
    ProviderMessage,
    ProviderMessageRole,
    ProviderToolDefinition,
)
from app.services.harness.providers.vertex_compiler import (
    VertexCompileError,
    VertexCompileErrorCode,
    VertexGenerateContentCompiler,
)
from tests.harness_provider_capability_fixtures import model, request


def vertex_model():
    return model().model_copy(
        update={
            "provider": "vertex",
            "model": "gemini-2.5-flash",
            "context_features": (),
        }
    )


def context(canonical_request=None) -> ProviderContextPlan:
    selected_request = canonical_request or request()
    return ProviderContextPlan(
        provider_request_sha256=hashlib.sha256(
            selected_request.model_dump_json().encode()
        ).hexdigest(),
        model_revision_sha256=vertex_model().model_revision_sha256,
        applied_features=(),
        estimated_input_tokens=10,
        reason="Compiled Vertex test context.",
    )


def tool() -> ProviderToolDefinition:
    schema_json = (
        '{"additionalProperties":false,"properties":{"city":'
        '{"type":"string"}},"required":["city"],"type":"object"}'
    )
    return ProviderToolDefinition(
        name="weather_lookup",
        version="1.0.0",
        description="Look up weather.",
        input_schema_json=schema_json,
        input_schema_sha256=hashlib.sha256(schema_json.encode()).hexdigest(),
    )


def test_compiler_maps_system_text_tools_and_reasoning_budget() -> None:
    canonical_request = request()
    system_message = ProviderMessage(
        role=ProviderMessageRole.SYSTEM,
        parts=canonical_request.messages[0].parts,
    )
    assistant_message = ProviderMessage(
        role=ProviderMessageRole.ASSISTANT,
        parts=canonical_request.messages[0].parts,
    )
    canonical_request = canonical_request.model_copy(
        update={
            "messages": (
                system_message,
                *canonical_request.messages,
                assistant_message,
            ),
            "tools": (tool(),),
            "reserved_reasoning_tokens": 256,
        }
    )

    compiled = VertexGenerateContentCompiler().compile(
        canonical_request,
        vertex_model(),
        context(canonical_request),
        tool_choice="weather_lookup",
    )
    wire = compiled.to_wire()

    assert wire["systemInstruction"] == {
        "parts": [{"text": "independent provider capabilities"}]
    }
    assert wire["contents"][0]["role"] == "user"
    assert wire["contents"][1]["role"] == "model"
    assert wire["generationConfig"] == {
        "maxOutputTokens": 1_280,
        "thinkingConfig": {
            "includeThoughts": True,
            "thinkingBudget": 256,
        },
    }
    declaration = wire["tools"][0]["functionDeclarations"][0]
    assert declaration["name"] == "weather_lookup"
    assert wire["toolConfig"]["functionCallingConfig"] == {
        "mode": "ANY",
        "allowedFunctionNames": ["weather_lookup"],
    }


def test_tool_history_rejects_missing_name_and_thought_signature() -> None:
    tool_result = ProviderMessage(
        role=ProviderMessageRole.TOOL,
        parts=request().messages[0].parts,
        tool_call_id="vertex_call_1",
    )
    canonical_request = request().model_copy(
        update={"messages": (*request().messages, tool_result)}
    )

    with pytest.raises(VertexCompileError) as captured:
        VertexGenerateContentCompiler().compile(
            canonical_request,
            vertex_model(),
            context(canonical_request),
        )

    assert captured.value.code is VertexCompileErrorCode.TOOL_HISTORY


def test_late_system_and_undeclared_tool_choice_fail_closed() -> None:
    late_system = ProviderMessage(
        role=ProviderMessageRole.SYSTEM,
        parts=request().messages[0].parts,
    )
    late_request = request().model_copy(
        update={"messages": (*request().messages, late_system)}
    )
    with pytest.raises(VertexCompileError) as late:
        VertexGenerateContentCompiler().compile(
            late_request,
            vertex_model(),
            context(late_request),
        )
    assert late.value.code is VertexCompileErrorCode.MESSAGE

    with pytest.raises(VertexCompileError) as undeclared:
        VertexGenerateContentCompiler().compile(
            request(),
            vertex_model(),
            context(),
            tool_choice="weather_lookup",
        )
    assert undeclared.value.code is VertexCompileErrorCode.TOOL_NAME


def test_non_object_tool_schema_is_rejected_before_egress() -> None:
    schema_json = '{"items":{"type":"string"},"type":"array"}'
    array_tool = ProviderToolDefinition(
        name="invalid_schema",
        version="1.0.0",
        description="Invalid for function arguments.",
        input_schema_json=schema_json,
        input_schema_sha256=hashlib.sha256(schema_json.encode()).hexdigest(),
    )
    canonical_request = request().model_copy(update={"tools": (array_tool,)})

    with pytest.raises(VertexCompileError) as captured:
        VertexGenerateContentCompiler().compile(
            canonical_request,
            vertex_model(),
            context(canonical_request),
        )

    assert captured.value.code is VertexCompileErrorCode.TOOL_SCHEMA
