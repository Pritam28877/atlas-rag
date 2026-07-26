"""Derive hashed local-compatible capability evidence from probe outcomes."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderCompleted,
    ProviderFinishReason,
    ProviderReasoningDelta,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderUsage,
)
from app.services.harness.providers.local_compatible_capabilities import (
    LocalCompatibleFeature,
)

if TYPE_CHECKING:
    from app.services.harness.providers.local_compatible_probe import (
        LocalProbeCaseResult,
    )


def observed_features(
    results: tuple[LocalProbeCaseResult, ...],
) -> tuple[LocalCompatibleFeature, ...]:
    from app.services.harness.providers.local_compatible_probe import (
        LocalProbeCase,
    )

    by_case = {result.case: result for result in results}
    features: set[LocalCompatibleFeature] = set()
    base = by_case[LocalProbeCase.BASE_STREAM]
    if (
        _has_event(base, ProviderTextDelta)
        and _has_event(base, ProviderUsage)
        and _completed(base, ProviderFinishReason.STOP)
    ):
        features.update(
            {
                LocalCompatibleFeature.MODALITY_TEXT,
                LocalCompatibleFeature.RESPONSES_STREAM,
                LocalCompatibleFeature.STORE_FALSE,
                LocalCompatibleFeature.STREAM_USAGE,
                LocalCompatibleFeature.TRUNCATION_DISABLED,
            }
        )
    cancelled = by_case[LocalProbeCase.CANCELLATION]
    if cancelled.transport_cancelled and _has_event(
        cancelled,
        ProviderCancelled,
    ):
        features.add(LocalCompatibleFeature.STREAM_CANCEL)
    if _completed(by_case[LocalProbeCase.DEVELOPER_ROLE]):
        features.add(LocalCompatibleFeature.ROLE_DEVELOPER)
    cache_usage = _usage(by_case[LocalProbeCase.PROMPT_CACHE])
    if cache_usage is not None and cache_usage.usage.cached_input_tokens > 0:
        features.add(LocalCompatibleFeature.PROMPT_CACHE_KEY)
    reasoning = by_case[LocalProbeCase.REASONING]
    reasoning_usage = _usage(reasoning)
    if (
        _has_event(reasoning, ProviderReasoningDelta)
        and reasoning_usage is not None
        and reasoning_usage.usage.reasoning_tokens > 0
    ):
        features.add(LocalCompatibleFeature.REASONING_STREAM)
    tools = by_case[LocalProbeCase.TOOLS]
    tool_calls = sum(isinstance(event, ProviderToolCall) for event in tools.events)
    if tool_calls and _completed(tools, ProviderFinishReason.TOOL_CALLS):
        features.update(
            {
                LocalCompatibleFeature.TOOLS_FUNCTION,
                LocalCompatibleFeature.TOOLS_STRICT,
            }
        )
        if tool_calls > 1:
            features.add(LocalCompatibleFeature.TOOLS_PARALLEL)
    return tuple(sorted(features))


def evidence_sha256(
    results: tuple[LocalProbeCaseResult, ...],
    features: tuple[LocalCompatibleFeature, ...],
) -> str:
    digest = hashlib.sha256()
    for result in results:
        digest.update(result.case.value.encode())
        digest.update(result.request_sha256.encode())
        digest.update(str(result.response_bytes).encode())
        digest.update(str(result.passed).encode())
        for event in result.events:
            digest.update(
                hashlib.sha256(event.model_dump_json().encode()).digest()
            )
    for feature in features:
        digest.update(feature.value.encode())
    return digest.hexdigest()


def _has_event(
    result: LocalProbeCaseResult,
    event_type: type[object],
) -> bool:
    return any(isinstance(event, event_type) for event in result.events)


def _completed(
    result: LocalProbeCaseResult,
    reason: ProviderFinishReason | None = None,
) -> bool:
    completed = next(
        (
            event
            for event in result.events
            if isinstance(event, ProviderCompleted)
        ),
        None,
    )
    return completed is not None and (
        reason is None or completed.finish_reason is reason
    )


def _usage(result: LocalProbeCaseResult) -> ProviderUsage | None:
    return next(
        (
            event
            for event in result.events
            if isinstance(event, ProviderUsage)
        ),
        None,
    )
