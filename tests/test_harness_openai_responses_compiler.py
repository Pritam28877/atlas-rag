import hashlib
import json

import pytest

from app.services.harness.protocol import (
    BlobPayload,
    CanonicalProviderRequest,
    ProviderContentPart,
    ProviderContextFeature,
    ProviderContextPlan,
    ProviderMessage,
    ProviderMessageRole,
    ProviderModality,
    ProviderToolDefinition,
)
from app.services.harness.providers import (
    OpenAICompileError,
    OpenAICompileErrorCode,
    OpenAIResponsesCompiler,
)
from tests.harness_provider_capability_fixtures import model, request


def context(
    canonical_request: CanonicalProviderRequest,
    *,
    features: tuple[ProviderContextFeature, ...] = (
        ProviderContextFeature.PROMPT_CACHE,
    ),
) -> ProviderContextPlan:
    return ProviderContextPlan(
        provider_request_sha256=hashlib.sha256(
            canonical_request.model_dump_json().encode()
        ).hexdigest(),
        model_revision_sha256=model().model_revision_sha256,
        applied_features=features,
        estimated_input_tokens=10,
        prompt_cache_key_sha256=(
            "9" * 64
            if ProviderContextFeature.PROMPT_CACHE in features
            else None
        ),
        state_reference_sha256=(
            "8" * 64
            if ProviderContextFeature.STATE_REFERENCE in features
            else None
        ),
        reason="Compiled test context.",
    )


def openai_model():
    return model().model_copy(update={"provider": "openai"})


def test_compiles_bounded_stateless_stream_request() -> None:
    canonical_request = request()

    compiled = OpenAIResponsesCompiler().compile(
        canonical_request,
        openai_model(),
        context(canonical_request),
    )

    assert compiled.model == model().model
    assert compiled.stream
    assert not compiled.store
    assert compiled.truncation == "disabled"
    assert compiled.max_output_tokens == 1_024
    assert compiled.prompt_cache_key == "9" * 64
    assert compiled.model_dump(mode="json")["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "independent provider capabilities",
                }
            ],
        }
    ]


def test_compiles_strict_tools_and_text_tool_results() -> None:
    schema = {
        "additionalProperties": False,
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "type": "object",
    }
    schema_json = json.dumps(schema, separators=(",", ":"), sort_keys=True)
    tool_output = request().messages[0].parts[0].payload
    canonical_request = request().model_copy(
        update={
            "messages": (
                request().messages[0],
                ProviderMessage(
                    role=ProviderMessageRole.TOOL,
                    tool_call_id="call_00000001",
                    parts=(
                        ProviderContentPart(
                            modality=ProviderModality.TEXT,
                            payload=tool_output,
                        ),
                    ),
                ),
            ),
            "tools": (
                ProviderToolDefinition(
                    name="workspace.read_file",
                    version="1.0.0",
                    description="Read a workspace file.",
                    input_schema_json=schema_json,
                    input_schema_sha256=hashlib.sha256(
                        schema_json.encode()
                    ).hexdigest(),
                ),
            ),
        }
    )

    compiled = OpenAIResponsesCompiler().compile(
        canonical_request,
        openai_model(),
        context(canonical_request),
    )

    assert compiled.parallel_tool_calls
    assert compiled.tools[0].strict
    assert compiled.tools[0].parameters == schema
    assert compiled.input[1].model_dump(mode="json") == {
        "type": "function_call_output",
        "call_id": "call_00000001",
        "output": "independent provider capabilities",
    }


@pytest.mark.parametrize(
    ("request_update", "model_update", "features", "expected"),
    (
        ({}, {"provider": "other"}, None, OpenAICompileErrorCode.MODEL),
        (
            {"reserved_reasoning_tokens": 4_000},
            {},
            None,
            OpenAICompileErrorCode.MODEL,
        ),
        (
            {},
            {},
            (ProviderContextFeature.STATE_REFERENCE,),
            OpenAICompileErrorCode.CONTEXT,
        ),
        (
            {"output_modalities": (ProviderModality.IMAGE,)},
            {},
            None,
            OpenAICompileErrorCode.MODALITY,
        ),
    ),
)
def test_model_context_and_output_mismatches_fail_closed(
    request_update: dict[str, object],
    model_update: dict[str, object],
    features: tuple[ProviderContextFeature, ...] | None,
    expected: OpenAICompileErrorCode,
) -> None:
    canonical_request = request().model_copy(update=request_update)
    provider_model = openai_model().model_copy(update=model_update)
    selected_features = (
        (ProviderContextFeature.PROMPT_CACHE,)
        if features is None
        else features
    )

    with pytest.raises(OpenAICompileError) as captured:
        OpenAIResponsesCompiler().compile(
            canonical_request,
            provider_model,
            context(canonical_request, features=selected_features),
        )

    assert captured.value.code is expected
    assert str(captured.value) == "OpenAI Responses request compilation failed"


def test_blob_payload_and_non_strict_tool_schema_fail_closed() -> None:
    blob_request = request().model_copy(
        update={
            "messages": (
                request().messages[0].model_copy(
                    update={
                        "parts": (
                            ProviderContentPart(
                                modality=ProviderModality.TEXT,
                                payload=BlobPayload(
                                    artifact_id="art_" + "1" * 32,
                                    media_type="text/plain",
                                    size_bytes=10,
                                    content_sha256="2" * 64,
                                ),
                            ),
                        )
                    }
                ),
            )
        }
    )
    invalid_schema = '{"properties":{"path":{"type":"string"}},"type":"object"}'
    invalid_tool_request = request().model_copy(
        update={
            "tools": (
                ProviderToolDefinition(
                    name="workspace.read_file",
                    version="1.0.0",
                    description="Read a workspace file.",
                    input_schema_json=invalid_schema,
                    input_schema_sha256=hashlib.sha256(
                        invalid_schema.encode()
                    ).hexdigest(),
                ),
            )
        }
    )

    with pytest.raises(OpenAICompileError) as blob_error:
        OpenAIResponsesCompiler().compile(
            blob_request,
            openai_model(),
            context(blob_request),
        )
    with pytest.raises(OpenAICompileError) as schema_error:
        OpenAIResponsesCompiler().compile(
            invalid_tool_request,
            openai_model(),
            context(invalid_tool_request),
        )

    assert blob_error.value.code is OpenAICompileErrorCode.PAYLOAD
    assert schema_error.value.code is OpenAICompileErrorCode.TOOL_SCHEMA
