"""Fresh capability evidence and explicit local route decisions."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    CanonicalProviderRequest,
    ProviderContextFeature,
    ProviderContextPlan,
    ProviderMessageRole,
    RouteId,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.routing import ModelName
from app.services.harness.providers.local_compatible_policy import (
    AuthorizedLocalCompatibleRoute,
)


class LocalCompatibleFeature(StrEnum):
    MODALITY_TEXT = "modality.text"
    PROMPT_CACHE_KEY = "prompt_cache.key"
    REASONING_STREAM = "reasoning.stream"
    RESPONSES_STREAM = "responses.stream"
    ROLE_DEVELOPER = "role.developer"
    STORE_FALSE = "store.false"
    STREAM_CANCEL = "stream.cancel"
    STREAM_USAGE = "stream.usage"
    TOOLS_FUNCTION = "tools.function"
    TOOLS_PARALLEL = "tools.parallel"
    TOOLS_STRICT = "tools.strict"
    TRUNCATION_DISABLED = "truncation.disabled"


class LocalCompatibleProbe(StrictProtocolModel):
    route_id: RouteId
    model_id: ModelName
    model_revision_sha256: Sha256
    destination_sha256: Sha256
    supported_features: tuple[LocalCompatibleFeature, ...] = Field(
        max_length=32,
    )
    evidence_sha256: Sha256
    observed_at: UtcTimestamp
    expires_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_probe(self) -> Self:
        if tuple(sorted(set(self.supported_features))) != self.supported_features:
            raise ValueError("local probe features must be unique and sorted")
        lifetime = self.expires_at - self.observed_at
        if not timedelta(seconds=1) <= lifetime <= timedelta(hours=1):
            raise ValueError("local probe lifetime must be between 1s and 1h")
        return self


class LocalCompatibleCapabilityDecision(StrictProtocolModel):
    route_id: RouteId
    provider_request_sha256: Sha256
    probe_evidence_sha256: Sha256
    required_features: tuple[LocalCompatibleFeature, ...] = Field(max_length=32)
    missing_features: tuple[LocalCompatibleFeature, ...] = Field(max_length=32)
    accepted: bool
    reason: str = Field(min_length=1, max_length=512)
    decided_at: UtcTimestamp
    valid_until: UtcTimestamp

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        for features, label in (
            (self.required_features, "required"),
            (self.missing_features, "missing"),
        ):
            if tuple(sorted(set(features))) != features:
                raise ValueError(f"local {label} features must be unique and sorted")
        if not set(self.missing_features).issubset(self.required_features):
            raise ValueError("missing local features were not required")
        if self.accepted == bool(self.missing_features):
            raise ValueError("local capability decision is inconsistent")
        if self.valid_until <= self.decided_at:
            raise ValueError("local capability decision must expire later")
        return self


class LocalCompatibleProbeErrorCode(StrEnum):
    BINDING = "binding"
    CLOCK = "clock"
    STALE = "stale"


class LocalCompatibleProbeError(RuntimeError):
    def __init__(self, code: LocalCompatibleProbeErrorCode) -> None:
        super().__init__("local-compatible capability evidence rejected")
        self.code = code


def decide_local_compatible_request(
    request: CanonicalProviderRequest,
    context: ProviderContextPlan,
    route: AuthorizedLocalCompatibleRoute,
    probe: LocalCompatibleProbe,
    *,
    decided_at: datetime,
) -> LocalCompatibleCapabilityDecision:
    request_sha256 = hashlib.sha256(
        request.model_dump_json().encode()
    ).hexdigest()
    if decided_at.tzinfo is None or decided_at.utcoffset() != timedelta(0):
        raise LocalCompatibleProbeError(LocalCompatibleProbeErrorCode.CLOCK)
    if not probe.observed_at <= decided_at < probe.expires_at:
        raise LocalCompatibleProbeError(LocalCompatibleProbeErrorCode.STALE)
    if (
        request.route_id != route.route_id
        or probe.route_id != route.route_id
        or probe.model_id != route.model_id
        or probe.destination_sha256 != route.destination_sha256
        or probe.model_revision_sha256 != context.model_revision_sha256
        or context.provider_request_sha256 != request_sha256
    ):
        raise LocalCompatibleProbeError(LocalCompatibleProbeErrorCode.BINDING)
    required = _required_features(request, context)
    supported = set(probe.supported_features)
    missing = tuple(feature for feature in required if feature not in supported)
    accepted = not missing
    return LocalCompatibleCapabilityDecision(
        route_id=route.route_id,
        provider_request_sha256=request_sha256,
        probe_evidence_sha256=probe.evidence_sha256,
        required_features=required,
        missing_features=missing,
        accepted=accepted,
        reason=(
            "Fresh probe supports every required local-compatible feature."
            if accepted
            else "Fresh probe is missing required local-compatible features."
        ),
        decided_at=decided_at,
        valid_until=probe.expires_at,
    )


def _required_features(
    request: CanonicalProviderRequest,
    context: ProviderContextPlan,
) -> tuple[LocalCompatibleFeature, ...]:
    required = {
        LocalCompatibleFeature.MODALITY_TEXT,
        LocalCompatibleFeature.RESPONSES_STREAM,
        LocalCompatibleFeature.STORE_FALSE,
        LocalCompatibleFeature.STREAM_CANCEL,
        LocalCompatibleFeature.STREAM_USAGE,
        LocalCompatibleFeature.TRUNCATION_DISABLED,
    }
    if request.tools:
        required.update(
            {
                LocalCompatibleFeature.TOOLS_FUNCTION,
                LocalCompatibleFeature.TOOLS_PARALLEL,
                LocalCompatibleFeature.TOOLS_STRICT,
            }
        )
    if request.reserved_reasoning_tokens > 0:
        required.add(LocalCompatibleFeature.REASONING_STREAM)
    if any(
        message.role is ProviderMessageRole.DEVELOPER
        for message in request.messages
    ):
        required.add(LocalCompatibleFeature.ROLE_DEVELOPER)
    if ProviderContextFeature.PROMPT_CACHE in context.applied_features:
        required.add(LocalCompatibleFeature.PROMPT_CACHE_KEY)
    return tuple(sorted(required))
