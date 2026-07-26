"""Capability-gated compiler for proven OpenAI Responses compatibility."""

from __future__ import annotations

import hashlib
from enum import StrEnum

from app.services.harness.protocol import (
    CanonicalProviderRequest,
    ProviderContextPlan,
    ProviderModelCapabilities,
)
from app.services.harness.providers.local_compatible_capabilities import (
    LocalCompatibleCapabilityDecision,
)
from app.services.harness.providers.openai_compiler import OpenAIResponsesCompiler
from app.services.harness.providers.openai_contracts import (
    CompiledOpenAIResponsesRequest,
)


class LocalCompatibleCompileErrorCode(StrEnum):
    CAPABILITY = "capability"
    EVIDENCE = "evidence"


class LocalCompatibleCompileError(ValueError):
    def __init__(self, code: LocalCompatibleCompileErrorCode) -> None:
        super().__init__("local-compatible request compilation failed")
        self.code = code


class LocalCompatibleResponsesCompiler:
    def __init__(self) -> None:
        self._delegate = OpenAIResponsesCompiler(provider="local-compatible")

    def compile(
        self,
        request: CanonicalProviderRequest,
        model: ProviderModelCapabilities,
        context: ProviderContextPlan,
        decision: LocalCompatibleCapabilityDecision,
    ) -> CompiledOpenAIResponsesRequest:
        request_sha256 = hashlib.sha256(
            request.model_dump_json().encode()
        ).hexdigest()
        if (
            decision.route_id != request.route_id
            or decision.provider_request_sha256 != request_sha256
            or decision.valid_until < request.deadline_at
        ):
            raise LocalCompatibleCompileError(
                LocalCompatibleCompileErrorCode.EVIDENCE
            )
        if not decision.accepted or decision.missing_features:
            raise LocalCompatibleCompileError(
                LocalCompatibleCompileErrorCode.CAPABILITY
            )
        return self._delegate.compile(request, model, context)
