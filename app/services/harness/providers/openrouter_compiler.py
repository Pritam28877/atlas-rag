"""Atlas-policy-bound compiler for OpenRouter Responses."""

from __future__ import annotations

from app.services.harness.protocol import (
    CanonicalProviderRequest,
    ProviderContextPlan,
    ProviderModelCapabilities,
)
from app.services.harness.providers.egress_contracts import SafeEgressHeader
from app.services.harness.providers.openai_compiler import (
    OpenAIResponsesCompiler,
)
from app.services.harness.providers.openrouter_contracts import (
    CompiledOpenRouterResponsesRequest,
    OpenRouterProviderPolicy,
)


class OpenRouterResponsesCompiler:
    def __init__(self, provider_policy: OpenRouterProviderPolicy) -> None:
        self._provider_policy = provider_policy
        self._openai_compiler = OpenAIResponsesCompiler(
            provider="openrouter"
        )

    def compile(
        self,
        request: CanonicalProviderRequest,
        model: ProviderModelCapabilities,
        context: ProviderContextPlan,
    ) -> CompiledOpenRouterResponsesRequest:
        compatible = self._openai_compiler.compile(
            request,
            model,
            context,
        )
        return CompiledOpenRouterResponsesRequest(
            **compatible.model_dump(mode="python"),
            provider=self._provider_policy,
        )

    def response_metadata_header(self) -> SafeEgressHeader:
        return SafeEgressHeader(
            name="x-openrouter-metadata",
            value="enabled",
        )
