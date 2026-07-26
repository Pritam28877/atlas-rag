"""Bounded stateful decoder for Vertex GenerateContent responses."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderCompleted,
    ProviderError,
    ProviderFailureClass,
    ProviderFinishReason,
    ProviderStreamEvent,
    ProviderTokenUsage,
    ProviderUsage,
)
from app.services.harness.providers.vertex_content_decoder import (
    VertexCandidateDecoder,
)
from app.services.harness.providers.vertex_contracts import VertexStreamMetadata
from app.services.harness.providers.vertex_decode_metadata import (
    VertexMetadataAccumulator,
)
from app.services.harness.providers.vertex_decode_support import (
    MALFORMED_FINISH_REASONS,
    MAXIMUM_VERTEX_WIRE_EVENT_BYTES,
    MAXIMUM_VERTEX_WIRE_EVENTS,
    POLICY_FINISH_REASONS,
    VertexDecodeError,
    VertexDecodeErrorCode,
    classify_api_error,
    mapping,
    parse_usage,
    require_allowed_keys,
    string,
)


class VertexGenerateContentDecoder:
    def __init__(
        self,
        cost_microusd: Callable[[int, int, int, int], int],
    ) -> None:
        self._cost_microusd = cost_microusd
        self._canonical_sequence = 1
        self._event_count = 0
        self._finish_reason: str | None = None
        self._terminal = False
        self._metadata = VertexMetadataAccumulator()
        self._candidate_decoder = VertexCandidateDecoder(
            self._next_sequence,
            self._metadata,
        )

    @property
    def metadata(self) -> VertexStreamMetadata | None:
        return self._metadata.metadata

    def decode(self, record: bytes) -> tuple[ProviderStreamEvent, ...]:
        try:
            response = self._parse(record)
            return self._decode_response(response)
        except VertexDecodeError:
            self._terminal = True
            raise
        except (OverflowError, TypeError, ValueError):
            self._terminal = True
            raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED) from None

    def cancel(self) -> ProviderCancelled:
        self._require_active()
        self._terminal = True
        return ProviderCancelled(
            sequence=self._next_sequence(),
            reason="Caller cancelled the Vertex provider stream.",
        )

    def _parse(self, record: bytes) -> dict[str, object]:
        self._require_active()
        if (
            not isinstance(record, bytes)
            or not 1 <= len(record) <= MAXIMUM_VERTEX_WIRE_EVENT_BYTES
        ):
            raise VertexDecodeError(VertexDecodeErrorCode.EVENT_SIZE)
        self._event_count += 1
        if self._event_count > MAXIMUM_VERTEX_WIRE_EVENTS:
            raise VertexDecodeError(VertexDecodeErrorCode.EVENT_LIMIT)
        try:
            response = json.loads(record)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED) from None
        if not isinstance(response, dict):
            raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED)
        return cast(dict[str, object], response)

    def _decode_response(
        self,
        response: dict[str, object],
    ) -> tuple[ProviderStreamEvent, ...]:
        if "error" in response:
            require_allowed_keys(response, frozenset({"error"}))
            return (self._api_error(mapping(response, "error")),)
        require_allowed_keys(
            response,
            frozenset(
                {
                    "candidates",
                    "createTime",
                    "modelVersion",
                    "promptFeedback",
                    "responseId",
                    "usageMetadata",
                }
            ),
        )
        self._metadata.record_identity(response)
        prompt_feedback = response.get("promptFeedback")
        if prompt_feedback is not None:
            if (
                not isinstance(prompt_feedback, dict)
                or response.get("candidates") not in (None, [])
                or "usageMetadata" in response
            ):
                raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED)
            return (
                self._prompt_block(cast(dict[str, object], prompt_feedback)),
            )
        events: list[ProviderStreamEvent] = []
        candidates = response.get("candidates", [])
        if not isinstance(candidates, list) or len(candidates) > 1:
            raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED)
        if candidates:
            candidate = candidates[0]
            if not isinstance(candidate, dict):
                raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED)
            events.extend(self._candidate(cast(dict[str, object], candidate)))
        usage = response.get("usageMetadata")
        if usage is not None:
            if not isinstance(usage, dict) or self._finish_reason is None:
                raise VertexDecodeError(VertexDecodeErrorCode.SEQUENCE)
            events.extend(self._complete(cast(dict[str, object], usage)))
        if not candidates and usage is None:
            raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED)
        return tuple(events)

    def _candidate(
        self,
        candidate: dict[str, object],
    ) -> tuple[ProviderStreamEvent, ...]:
        if self._finish_reason is not None:
            raise VertexDecodeError(VertexDecodeErrorCode.SEQUENCE)
        events, finish_reason = self._candidate_decoder.decode(candidate)
        self._finish_reason = finish_reason
        return events

    def _complete(
        self,
        usage: dict[str, object],
    ) -> tuple[ProviderStreamEvent, ...]:
        require_allowed_keys(
            usage,
            frozenset(
                {
                    "cacheTokensDetails",
                    "cachedContentTokenCount",
                    "candidatesTokenCount",
                    "candidatesTokensDetails",
                    "promptTokenCount",
                    "promptTokensDetails",
                    "thoughtsTokenCount",
                    "toolUsePromptTokenCount",
                    "toolUsePromptTokensDetails",
                    "totalTokenCount",
                    "trafficType",
                }
            ),
        )
        counts = parse_usage(usage)
        usage_event = ProviderUsage(
            sequence=self._next_sequence(),
            usage=ProviderTokenUsage(
                input_tokens=counts.input_tokens,
                cached_input_tokens=counts.cached_tokens,
                output_tokens=counts.output_tokens,
                reasoning_tokens=counts.reasoning_tokens,
                cost_microusd=self._cost_microusd(
                    counts.input_tokens,
                    counts.cached_tokens,
                    counts.output_tokens,
                    counts.reasoning_tokens,
                ),
            ),
        )
        terminal = self._terminal_event()
        finish_reason = cast(str, self._finish_reason)
        self._metadata.finalize(finish_reason, counts)
        self._terminal = True
        return (usage_event, terminal)

    def _terminal_event(self) -> ProviderCompleted | ProviderError:
        reason = self._finish_reason
        if reason == "STOP":
            return ProviderCompleted(
                sequence=self._next_sequence(),
                finish_reason=(
                    ProviderFinishReason.TOOL_CALLS
                    if self._candidate_decoder.tool_calls
                    else ProviderFinishReason.STOP
                ),
            )
        if reason == "MAX_TOKENS":
            return ProviderCompleted(
                sequence=self._next_sequence(),
                finish_reason=ProviderFinishReason.LENGTH,
            )
        failure_class = (
            ProviderFailureClass.POLICY
            if reason in POLICY_FINISH_REASONS
            else ProviderFailureClass.MALFORMED
        )
        if reason not in POLICY_FINISH_REASONS | MALFORMED_FINISH_REASONS:
            failure_class = ProviderFailureClass.INTERNAL
        return ProviderError(
            sequence=self._next_sequence(),
            failure_class=failure_class,
            retry_allowed=False,
            reason="Vertex stopped with a classified terminal reason.",
        )

    def _prompt_block(
        self,
        feedback: dict[str, object],
    ) -> ProviderError:
        require_allowed_keys(
            feedback,
            frozenset(
                {"blockReason", "blockReasonMessage", "safetyRatings"}
            ),
        )
        string(feedback, "blockReason", maximum=128)
        self._metadata.record_safety(feedback)
        self._terminal = True
        return ProviderError(
            sequence=self._next_sequence(),
            failure_class=ProviderFailureClass.POLICY,
            retry_allowed=False,
            reason="Vertex rejected the prompt under provider policy.",
        )

    def _api_error(self, error: dict[str, object]) -> ProviderError:
        require_allowed_keys(
            error,
            frozenset({"code", "details", "message", "status"}),
        )
        status = string(error, "status", maximum=128)
        failure_class, retry_allowed = classify_api_error(status)
        self._terminal = True
        return ProviderError(
            sequence=self._next_sequence(),
            failure_class=failure_class,
            retry_allowed=retry_allowed,
            reason="Vertex returned a classified API error.",
        )

    def _next_sequence(self) -> int:
        sequence_number = self._canonical_sequence
        self._canonical_sequence += 1
        return sequence_number

    def _require_active(self) -> None:
        if self._terminal:
            raise VertexDecodeError(VertexDecodeErrorCode.TERMINAL)
