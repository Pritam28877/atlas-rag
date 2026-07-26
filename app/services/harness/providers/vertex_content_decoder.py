"""Vertex candidate content normalization with bounded tool-call state."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import cast

from app.services.harness.protocol import (
    ProviderReasoningDelta,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
)
from app.services.harness.providers.vertex_decode_metadata import (
    VertexMetadataAccumulator,
)
from app.services.harness.providers.vertex_decode_support import (
    MAXIMUM_VERTEX_FUNCTION_CALLS,
    MAXIMUM_VERTEX_TOOL_ARGUMENT_BYTES,
    VertexDecodeError,
    VertexDecodeErrorCode,
    canonical_json,
    integer,
    mapping,
    require_allowed_keys,
    sequence,
    string,
)


class VertexCandidateDecoder:
    def __init__(
        self,
        next_sequence: Callable[[], int],
        metadata: VertexMetadataAccumulator,
    ) -> None:
        self._next_sequence = next_sequence
        self._metadata = metadata
        self._tool_calls = 0

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    def decode(
        self,
        candidate: dict[str, object],
    ) -> tuple[tuple[ProviderStreamEvent, ...], str | None]:
        if integer(candidate, "index") != 0:
            raise VertexDecodeError(VertexDecodeErrorCode.SEQUENCE)
        require_allowed_keys(
            candidate,
            frozenset(
                {
                    "content",
                    "finishMessage",
                    "finishReason",
                    "index",
                    "safetyRatings",
                }
            ),
        )
        safety = {
            key: candidate[key]
            for key in ("finishMessage", "safetyRatings")
            if key in candidate
        }
        if safety:
            self._metadata.record_safety(safety)
        events: tuple[ProviderStreamEvent, ...] = ()
        content = candidate.get("content")
        if content is not None:
            if not isinstance(content, dict):
                raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED)
            events = self._content(cast(dict[str, object], content))
        finish_reason = candidate.get("finishReason")
        if finish_reason in {None, "", "FINISH_REASON_UNSPECIFIED"}:
            return events, None
        return events, string(candidate, "finishReason", maximum=128)

    def _content(
        self,
        content: dict[str, object],
    ) -> tuple[ProviderStreamEvent, ...]:
        require_allowed_keys(content, frozenset({"parts", "role"}))
        if content.get("role") != "model":
            raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED)
        events: list[ProviderStreamEvent] = []
        for raw_part in sequence(content, "parts"):
            if not isinstance(raw_part, dict):
                raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED)
            events.extend(self._part(cast(dict[str, object], raw_part)))
        return tuple(events)

    def _part(
        self,
        part: dict[str, object],
    ) -> tuple[ProviderStreamEvent, ...]:
        signature = part.get("thoughtSignature")
        if signature is not None:
            raw_signature = string(part, "thoughtSignature", maximum=64 * 1024)
            self._metadata.record_thought_signature(raw_signature)
        has_text = "text" in part
        has_function = "functionCall" in part
        if has_text == has_function:
            raise VertexDecodeError(VertexDecodeErrorCode.UNSUPPORTED)
        if has_function:
            require_allowed_keys(
                part,
                frozenset({"functionCall", "thoughtSignature"}),
            )
            return (self._tool_call(mapping(part, "functionCall")),)
        require_allowed_keys(
            part,
            frozenset({"text", "thought", "thoughtSignature"}),
        )
        text = string(part, "text", maximum=32 * 1024, allow_empty=True)
        if not text:
            if signature is None:
                raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED)
            return ()
        if part.get("thought", False) is True:
            return (
                ProviderReasoningDelta(
                    sequence=self._next_sequence(),
                    text=text,
                ),
            )
        if part.get("thought", False) is not False:
            raise VertexDecodeError(VertexDecodeErrorCode.MALFORMED)
        return (
            ProviderTextDelta(
                sequence=self._next_sequence(),
                text=text,
            ),
        )

    def _tool_call(self, function_call: dict[str, object]) -> ProviderToolCall:
        if self._tool_calls >= MAXIMUM_VERTEX_FUNCTION_CALLS:
            raise VertexDecodeError(VertexDecodeErrorCode.EVENT_LIMIT)
        require_allowed_keys(function_call, frozenset({"args", "name"}))
        name = string(function_call, "name", maximum=64)
        arguments = mapping(function_call, "args")
        canonical_arguments = canonical_json(arguments)
        if len(canonical_arguments.encode()) > MAXIMUM_VERTEX_TOOL_ARGUMENT_BYTES:
            raise VertexDecodeError(VertexDecodeErrorCode.EVENT_SIZE)
        self._tool_calls += 1
        identity = canonical_json(
            {
                "arguments": arguments,
                "name": name,
                "ordinal": self._tool_calls,
                "response": self._metadata.response_id_sha256,
            }
        )
        call_id = "vertex_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        return ProviderToolCall(
            sequence=self._next_sequence(),
            call_id=call_id,
            tool_name=name,
            arguments_json=canonical_arguments,
            arguments_sha256=hashlib.sha256(
                canonical_arguments.encode()
            ).hexdigest(),
        )
