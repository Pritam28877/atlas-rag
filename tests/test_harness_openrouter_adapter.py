import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import (
    ProviderCompleted,
    ProviderContextFeature,
    ProviderContextPlan,
    ProviderFailureClass,
    ProviderTextDelta,
    ProviderUsage,
)
from app.services.harness.providers import (
    OpenAIDecodeError,
    OpenAIDecodeErrorCode,
    OpenRouterProviderPolicy,
    OpenRouterResponsesCompiler,
    OpenRouterResponsesDecoder,
    compile_routing_metadata,
)
from tests.harness_provider_capability_fixtures import model, request

FIXTURES = (
    Path(__file__).parent / "fixtures" / "harness" / "openrouter_responses"
)


def context() -> ProviderContextPlan:
    canonical_request = request()
    return ProviderContextPlan(
        provider_request_sha256=hashlib.sha256(
            canonical_request.model_dump_json().encode()
        ).hexdigest(),
        model_revision_sha256=model().model_revision_sha256,
        applied_features=(ProviderContextFeature.PROMPT_CACHE,),
        estimated_input_tokens=10,
        prompt_cache_key_sha256="9" * 64,
        reason="Compiled OpenRouter test context.",
    )


def routing_policy() -> OpenRouterProviderPolicy:
    return OpenRouterProviderPolicy(
        order=("approved-endpoint", "approved-endpoint/turbo"),
        only=("approved-endpoint", "approved-endpoint/turbo"),
    )


def test_compiler_binds_openrouter_routing_and_data_policy() -> None:
    compiler = OpenRouterResponsesCompiler(routing_policy())
    provider_model = model().model_copy(
        update={
            "provider": "openrouter",
            "model": "openai/gpt-4o",
        }
    )

    compiled = compiler.compile(request(), provider_model, context())

    assert compiled.model == "openai/gpt-4o"
    assert compiled.provider.order == routing_policy().only
    assert not compiled.provider.allow_fallbacks
    assert compiled.provider.require_parameters
    assert compiled.provider.data_collection == "deny"
    assert compiled.provider.zdr
    assert compiler.response_metadata_header().model_dump() == {
        "name": "x-openrouter-metadata",
        "value": "enabled",
    }


def test_routing_policy_cannot_relax_or_escape_allowlist() -> None:
    with pytest.raises(ValueError, match="equal"):
        OpenRouterProviderPolicy(
            order=("approved-endpoint",),
            only=("other-endpoint",),
        )
    with pytest.raises(ValueError, match="unique"):
        OpenRouterProviderPolicy(
            order=("approved-endpoint", "approved-endpoint"),
            only=("approved-endpoint", "approved-endpoint"),
        )
    with pytest.raises(ValidationError):
        OpenRouterProviderPolicy(
            order=("approved-endpoint",),
            only=("approved-endpoint",),
            allow_fallbacks=True,
        )


def test_documented_beta_stream_and_metadata_are_normalized() -> None:
    decoder = OpenRouterResponsesDecoder(
        lambda input_tokens, cached, output_tokens, reasoning: (
            input_tokens + output_tokens + reasoning
        )
    )
    events = []
    for record in (FIXTURES / "happy.jsonl").read_bytes().splitlines():
        events.extend(decoder.decode(record))

    assert isinstance(events[0], ProviderTextDelta)
    assert events[0].text == "OpenRouter "
    assert isinstance(events[1], ProviderTextDelta)
    assert events[1].text == "response."
    assert isinstance(events[2], ProviderUsage)
    assert events[2].usage.cost_microusd == 16
    assert isinstance(events[3], ProviderCompleted)
    assert decoder.routing_metadata is not None
    assert decoder.routing_metadata.canonical_json == (
        '{"latency_ms":42,"provider_name":"approved-endpoint"}'
    )


def test_midstream_error_is_redacted_and_done_requires_terminal() -> None:
    decoder = OpenRouterResponsesDecoder(lambda *counts: 0)
    records = (FIXTURES / "rate_limit.jsonl").read_bytes().splitlines()

    events = decoder.decode(records[0])

    assert events[0].failure_class is ProviderFailureClass.RATE_LIMIT
    assert "fixture raw message" not in events[0].reason
    assert decoder.decode(records[1]) == ()

    early_done = OpenRouterResponsesDecoder(lambda *counts: 0)
    with pytest.raises(OpenAIDecodeError) as captured:
        early_done.decode(b"[DONE]")
    assert captured.value.code is OpenAIDecodeErrorCode.SEQUENCE


def test_sensitive_or_changed_routing_metadata_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsafe keys"):
        compile_routing_metadata({"provider": "safe", "prompt": "secret"})

    decoder = OpenRouterResponsesDecoder(lambda *counts: 0)
    first = (
        b'{"openrouter_metadata":{"provider_name":"one"},'
        b'"response":{"status":"in_progress"},"type":"response.created"}'
    )
    second = (
        b'{"openrouter_metadata":{"provider_name":"two"},'
        b'"response":{"status":"in_progress"},"type":"response.in_progress"}'
    )
    assert decoder.decode(first) == ()
    with pytest.raises(OpenAIDecodeError) as captured:
        decoder.decode(second)
    assert captured.value.code is OpenAIDecodeErrorCode.MALFORMED
