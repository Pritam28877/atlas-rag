"""Deterministic canonical text and tool requests for the Bedrock smoke."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from app.cli.harness.bedrock_smoke_contracts import AuthorizedBedrockSmoke
from app.services.harness.protocol import (
    CanonicalProviderRequest,
    DataClassification,
    InlinePayload,
    ProviderContentPart,
    ProviderContextPlan,
    ProviderMessage,
    ProviderMessageRole,
    ProviderModality,
    ProviderModelCapabilities,
    ProviderToolDefinition,
)
from app.services.harness.providers import (
    AuthorizedBedrockRoute,
    BedrockConverseStreamCompiler,
    CompiledBedrockConverseStreamRequest,
)

BEDROCK_SMOKE_EXPECTED_TEXT = "ATLAS_SMOKE_OK"
BEDROCK_SMOKE_TOOL_NAME = "atlas_smoke_report"
BEDROCK_SMOKE_TEXT_PROMPT = (
    f"Reply with exactly {BEDROCK_SMOKE_EXPECTED_TEXT}."
)
BEDROCK_SMOKE_TOOL_PROMPT = (
    f"Call {BEDROCK_SMOKE_TOOL_NAME} exactly once with status "
    f"{BEDROCK_SMOKE_EXPECTED_TEXT}. Do not answer with text."
)
BEDROCK_SMOKE_TOOL_ARGUMENTS = json.dumps(
    {"status": BEDROCK_SMOKE_EXPECTED_TEXT},
    separators=(",", ":"),
    sort_keys=True,
)
BEDROCK_SMOKE_TOOL_SCHEMA = json.dumps(
    {
        "additionalProperties": False,
        "properties": {
            "status": {
                "const": BEDROCK_SMOKE_EXPECTED_TEXT,
                "type": "string",
            }
        },
        "required": ["status"],
        "type": "object",
    },
    separators=(",", ":"),
    sort_keys=True,
)


@dataclass(frozen=True, slots=True)
class CompiledBedrockSmokeRequests:
    text: CompiledBedrockConverseStreamRequest
    tool: CompiledBedrockConverseStreamRequest
    text_sha256: str
    tool_sha256: str
    text_request_id: str
    tool_request_id: str
    turn_id: str
    workspace_id: str


def build_bedrock_smoke_requests(
    authorized: AuthorizedBedrockSmoke,
    route: AuthorizedBedrockRoute,
    model: ProviderModelCapabilities,
    *,
    deadline_at: datetime,
) -> CompiledBedrockSmokeRequests:
    authorization_id = authorized.grant.authorization_id
    turn_id = _identifier("trn", authorization_id, "turn")
    workspace_id = _identifier("wsp", authorization_id, "workspace")
    text_request_id = _identifier("req", authorization_id, "text")
    tool_request_id = _identifier("req", authorization_id, "tool")
    text = _compile(
        route,
        model,
        request_id=text_request_id,
        turn_id=turn_id,
        prompt=BEDROCK_SMOKE_TEXT_PROMPT,
        max_output_tokens=authorized.grant.text_max_output_tokens,
        deadline_at=deadline_at,
        tools=(),
        tool_choice=None,
    )
    tool_definition = _tool_definition()
    tool = _compile(
        route,
        model,
        request_id=tool_request_id,
        turn_id=turn_id,
        prompt=BEDROCK_SMOKE_TOOL_PROMPT,
        max_output_tokens=authorized.grant.tool_max_output_tokens,
        deadline_at=deadline_at,
        tools=(tool_definition,),
        tool_choice=BEDROCK_SMOKE_TOOL_NAME,
    )
    return CompiledBedrockSmokeRequests(
        text=text,
        tool=tool,
        text_sha256=_compiled_sha256(text),
        tool_sha256=_compiled_sha256(tool),
        text_request_id=text_request_id,
        tool_request_id=tool_request_id,
        turn_id=turn_id,
        workspace_id=workspace_id,
    )


def _compile(
    route: AuthorizedBedrockRoute,
    model: ProviderModelCapabilities,
    *,
    request_id: str,
    turn_id: str,
    prompt: str,
    max_output_tokens: int,
    deadline_at: datetime,
    tools: tuple[ProviderToolDefinition, ...],
    tool_choice: str | None,
) -> CompiledBedrockConverseStreamRequest:
    encoded_prompt = prompt.encode()
    request = CanonicalProviderRequest(
        request_id=request_id,
        turn_id=turn_id,
        route_id=route.route_id,
        classification=DataClassification.PUBLIC,
        messages=(
            ProviderMessage(
                role=ProviderMessageRole.USER,
                parts=(
                    ProviderContentPart(
                        modality=ProviderModality.TEXT,
                        payload=InlinePayload(
                            text=prompt,
                            size_bytes=len(encoded_prompt),
                            content_sha256=hashlib.sha256(
                                encoded_prompt
                            ).hexdigest(),
                        ),
                    ),
                ),
            ),
        ),
        tools=tools,
        required_capabilities=(),
        output_modalities=(ProviderModality.TEXT,),
        reserved_output_tokens=max_output_tokens,
        reserved_reasoning_tokens=0,
        deadline_at=deadline_at,
    )
    context = ProviderContextPlan(
        provider_request_sha256=hashlib.sha256(
            request.model_dump_json().encode()
        ).hexdigest(),
        model_revision_sha256=model.model_revision_sha256,
        applied_features=(),
        estimated_input_tokens=32,
        reason="Signed Bedrock smoke uses bounded stateless context.",
    )
    return BedrockConverseStreamCompiler().compile(
        request,
        model,
        context,
        tool_choice=tool_choice,
    )


def _tool_definition() -> ProviderToolDefinition:
    return ProviderToolDefinition(
        name=BEDROCK_SMOKE_TOOL_NAME,
        version="1",
        description="Reports the fixed Atlas Bedrock smoke status.",
        input_schema_json=BEDROCK_SMOKE_TOOL_SCHEMA,
        input_schema_sha256=hashlib.sha256(
            BEDROCK_SMOKE_TOOL_SCHEMA.encode()
        ).hexdigest(),
    )


def _compiled_sha256(
    request: CompiledBedrockConverseStreamRequest,
) -> str:
    return hashlib.sha256(request.model_dump_json().encode()).hexdigest()


def _identifier(prefix: str, authorization_id: str, purpose: str) -> str:
    digest = hashlib.sha256(
        f"{authorization_id}:{purpose}".encode()
    ).hexdigest()
    return f"{prefix}_{digest[:32]}"
