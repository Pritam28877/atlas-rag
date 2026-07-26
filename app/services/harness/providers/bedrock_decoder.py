"""Bounded stateful decoder for Bedrock ConverseStream events."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderCompleted,
    ProviderError,
    ProviderFailureClass,
    ProviderReasoningDelta,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderTokenUsage,
    ProviderToolCall,
    ProviderUsage,
)
from app.services.harness.providers.bedrock_contracts import (
    BedrockStreamMetadata,
)
from app.services.harness.providers.bedrock_decode_support import (
    FINISH_REASONS,
    MAXIMUM_BEDROCK_BLOCKS,
    MAXIMUM_BEDROCK_EVENT_BYTES,
    MAXIMUM_BEDROCK_EVENTS,
    MAXIMUM_BEDROCK_TOOL_ARGUMENT_BYTES,
    STREAM_ERROR_CLASSIFICATION,
    BedrockDecodeError,
    BedrockDecodeErrorCode,
    canonical_arguments,
    content_index,
    integer,
    mapping,
    metadata_sha256,
    optional_integer,
    string,
)


@dataclass(slots=True)
class _ToolBlock:
    call_id: str
    name: str
    arguments: str = ""


class BedrockConverseStreamDecoder:
    def __init__(
        self,
        cost_microusd: Callable[[int, int, int, int], int],
    ) -> None:
        self._cost_microusd = cost_microusd
        self._sequence = 1
        self._event_count = 0
        self._message_started = False
        self._message_stopped = False
        self._terminal = False
        self._stop_reason: str | None = None
        self._tools: dict[int, _ToolBlock] = {}
        self._open_blocks: set[int] = set()
        self._reasoning_signature = hashlib.sha256()
        self._has_reasoning_signature = False
        self._provider_metadata: dict[str, object] = {}
        self._metadata: BedrockStreamMetadata | None = None

    @property
    def metadata(self) -> BedrockStreamMetadata | None:
        return self._metadata

    def decode(
        self,
        event: dict[str, object],
    ) -> tuple[ProviderStreamEvent, ...]:
        try:
            return self._decode_event(event)
        except BedrockDecodeError:
            self._terminal = True
            raise
        except (TypeError, ValueError):
            self._terminal = True
            raise BedrockDecodeError(BedrockDecodeErrorCode.MALFORMED) from None

    def _decode_event(
        self,
        event: dict[str, object],
    ) -> tuple[ProviderStreamEvent, ...]:
        self._require_active()
        values = self._parse(event)
        event_name, payload = next(iter(values.items()))
        if event_name == "messageStart":
            self._message_start(payload)
            return ()
        if event_name == "contentBlockStart":
            self._content_start(payload)
            return ()
        if event_name == "contentBlockDelta":
            return self._content_delta(payload)
        if event_name == "contentBlockStop":
            return self._content_stop(payload)
        if event_name == "messageStop":
            self._message_stop(payload)
            return ()
        if event_name == "metadata":
            return self._complete(payload)
        if event_name in STREAM_ERROR_CLASSIFICATION:
            return (self._stream_error(event_name),)
        raise BedrockDecodeError(BedrockDecodeErrorCode.UNSUPPORTED)

    def cancel(self) -> ProviderCancelled:
        self._require_active()
        self._terminal = True
        return ProviderCancelled(
            sequence=self._next_sequence(),
            reason="Caller cancelled the Bedrock provider stream.",
        )

    def _parse(
        self,
        event: dict[str, object],
    ) -> dict[str, dict[str, object]]:
        if not isinstance(event, dict) or len(event) != 1:
            raise BedrockDecodeError(BedrockDecodeErrorCode.MALFORMED)
        try:
            encoded = json.dumps(
                event,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        except (TypeError, ValueError):
            raise BedrockDecodeError(BedrockDecodeErrorCode.MALFORMED) from None
        if not 1 <= len(encoded) <= MAXIMUM_BEDROCK_EVENT_BYTES:
            raise BedrockDecodeError(BedrockDecodeErrorCode.EVENT_SIZE)
        self._event_count += 1
        if self._event_count > MAXIMUM_BEDROCK_EVENTS:
            raise BedrockDecodeError(BedrockDecodeErrorCode.EVENT_LIMIT)
        name, payload = next(iter(event.items()))
        if not isinstance(name, str) or not isinstance(payload, dict):
            raise BedrockDecodeError(BedrockDecodeErrorCode.MALFORMED)
        return {name: cast(dict[str, object], payload)}

    def _message_start(self, payload: dict[str, object]) -> None:
        if self._message_started or payload.get("role") != "assistant":
            raise BedrockDecodeError(BedrockDecodeErrorCode.SEQUENCE)
        self._message_started = True

    def _content_start(self, payload: dict[str, object]) -> None:
        self._require_message_content()
        index = content_index(payload)
        if (
            index in self._open_blocks
            or len(self._open_blocks) >= MAXIMUM_BEDROCK_BLOCKS
        ):
            raise BedrockDecodeError(BedrockDecodeErrorCode.SEQUENCE)
        start = mapping(payload, "start")
        tool = mapping(start, "toolUse")
        call_id = string(tool, "toolUseId", 128)
        name = string(tool, "name", 128)
        self._tools[index] = _ToolBlock(call_id=call_id, name=name)
        self._open_blocks.add(index)

    def _content_delta(
        self,
        payload: dict[str, object],
    ) -> tuple[ProviderStreamEvent, ...]:
        self._require_message_content()
        index = content_index(payload)
        delta = mapping(payload, "delta")
        if "text" in delta:
            self._open_blocks.add(index)
            text = delta.get("text")
            if not isinstance(text, str) or len(text) > 32 * 1024:
                raise BedrockDecodeError(BedrockDecodeErrorCode.MALFORMED)
            if not text:
                return ()
            return (
                ProviderTextDelta(
                    sequence=self._next_sequence(),
                    text=text,
                ),
            )
        if "reasoningContent" in delta:
            self._open_blocks.add(index)
            return self._reasoning_delta(mapping(delta, "reasoningContent"))
        if "toolUse" in delta:
            tool = self._tools.get(index)
            if tool is None:
                raise BedrockDecodeError(BedrockDecodeErrorCode.SEQUENCE)
            fragment = string(
                mapping(delta, "toolUse"),
                "input",
                32 * 1024,
            )
            argument_bytes = len((tool.arguments + fragment).encode())
            if argument_bytes > MAXIMUM_BEDROCK_TOOL_ARGUMENT_BYTES:
                raise BedrockDecodeError(BedrockDecodeErrorCode.EVENT_SIZE)
            tool.arguments += fragment
            return ()
        raise BedrockDecodeError(BedrockDecodeErrorCode.UNSUPPORTED)

    def _reasoning_delta(
        self,
        reasoning: dict[str, object],
    ) -> tuple[ProviderStreamEvent, ...]:
        if "text" in reasoning:
            return (
                ProviderReasoningDelta(
                    sequence=self._next_sequence(),
                    text=string(reasoning, "text", 32 * 1024),
                ),
            )
        if "signature" in reasoning:
            signature = string(reasoning, "signature", 32 * 1024)
            self._reasoning_signature.update(signature.encode())
            self._has_reasoning_signature = True
            return ()
        raise BedrockDecodeError(BedrockDecodeErrorCode.UNSUPPORTED)

    def _content_stop(
        self,
        payload: dict[str, object],
    ) -> tuple[ProviderStreamEvent, ...]:
        self._require_message_content()
        index = content_index(payload)
        if index not in self._open_blocks:
            raise BedrockDecodeError(BedrockDecodeErrorCode.SEQUENCE)
        self._open_blocks.remove(index)
        tool = self._tools.pop(index, None)
        if tool is None:
            return ()
        arguments = canonical_arguments(tool.arguments)
        return (
            ProviderToolCall(
                sequence=self._next_sequence(),
                call_id=tool.call_id,
                tool_name=tool.name,
                arguments_json=arguments,
                arguments_sha256=hashlib.sha256(arguments.encode()).hexdigest(),
            ),
        )

    def _message_stop(self, payload: dict[str, object]) -> None:
        if not self._message_started or self._message_stopped or self._open_blocks:
            raise BedrockDecodeError(BedrockDecodeErrorCode.SEQUENCE)
        self._stop_reason = string(payload, "stopReason", 128)
        additional = payload.get("additionalModelResponseFields")
        if additional is not None:
            self._provider_metadata["additionalModelResponseFields"] = additional
        self._message_stopped = True

    def _complete(
        self,
        payload: dict[str, object],
    ) -> tuple[ProviderStreamEvent, ...]:
        if not self._message_stopped or self._stop_reason is None:
            raise BedrockDecodeError(BedrockDecodeErrorCode.SEQUENCE)
        usage = mapping(payload, "usage")
        metrics = mapping(payload, "metrics")
        input_tokens = integer(usage, "inputTokens")
        output_tokens = integer(usage, "outputTokens")
        cached_tokens = optional_integer(usage, "cacheReadInputTokens")
        cache_write_tokens = optional_integer(
            usage,
            "cacheWriteInputTokens",
        )
        latency_ms = integer(metrics, "latencyMs")
        for key in ("trace", "performanceConfig", "serviceTier"):
            if key in payload:
                self._provider_metadata[key] = payload[key]
        provider_metadata_sha256 = metadata_sha256(self._provider_metadata)
        self._metadata = BedrockStreamMetadata(
            stop_reason=self._stop_reason,
            latency_ms=latency_ms,
            cache_write_input_tokens=cache_write_tokens,
            reasoning_signature_sha256=(
                self._reasoning_signature.hexdigest()
                if self._has_reasoning_signature
                else None
            ),
            provider_metadata_sha256=provider_metadata_sha256,
        )
        usage_event = ProviderUsage(
            sequence=self._next_sequence(),
            usage=ProviderTokenUsage(
                input_tokens=input_tokens,
                cached_input_tokens=cached_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=0,
                cost_microusd=self._cost_microusd(
                    input_tokens,
                    cached_tokens,
                    output_tokens,
                    0,
                ),
            ),
        )
        terminal = self._terminal_event(self._stop_reason)
        self._terminal = True
        return (usage_event, terminal)

    def _terminal_event(
        self,
        stop_reason: str,
    ) -> ProviderCompleted | ProviderError:
        finish_reason = FINISH_REASONS.get(stop_reason)
        if finish_reason is not None:
            return ProviderCompleted(
                sequence=self._next_sequence(),
                finish_reason=finish_reason,
            )
        failure_class = (
            ProviderFailureClass.POLICY
            if stop_reason in {"guardrail_intervened", "content_filtered"}
            else (
                ProviderFailureClass.CONTEXT_LENGTH
                if stop_reason == "model_context_window_exceeded"
                else ProviderFailureClass.MALFORMED
            )
        )
        return ProviderError(
            sequence=self._next_sequence(),
            failure_class=failure_class,
            retry_allowed=False,
            reason="Bedrock stopped with a classified terminal reason.",
        )

    def _stream_error(self, event_name: str) -> ProviderError:
        failure_class, retry_allowed = STREAM_ERROR_CLASSIFICATION[event_name]
        self._terminal = True
        return ProviderError(
            sequence=self._next_sequence(),
            failure_class=failure_class,
            retry_allowed=retry_allowed,
            reason="Bedrock returned a classified stream error.",
        )

    def _require_message_content(self) -> None:
        if not self._message_started or self._message_stopped:
            raise BedrockDecodeError(BedrockDecodeErrorCode.SEQUENCE)

    def _require_active(self) -> None:
        if self._terminal:
            raise BedrockDecodeError(BedrockDecodeErrorCode.TERMINAL)

    def _next_sequence(self) -> int:
        sequence = self._sequence
        self._sequence += 1
        return sequence
