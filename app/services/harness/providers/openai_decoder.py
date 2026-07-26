"""Bounded stateful decoder for supported OpenAI Responses stream events."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import cast

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderCompleted,
    ProviderError,
    ProviderFinishReason,
    ProviderReasoningDelta,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderTokenUsage,
    ProviderToolCall,
    ProviderUsage,
)
from app.services.harness.providers.openai_decode_support import (
    MAXIMUM_OPENAI_FUNCTION_CALLS,
    MAXIMUM_OPENAI_WIRE_EVENT_BYTES,
    MAXIMUM_OPENAI_WIRE_EVENTS,
    OpenAIDecodeError,
    OpenAIDecodeErrorCode,
    canonical_arguments,
    classify_error,
    integer,
    mapping,
    optional_nested_integer,
    string,
)

_IGNORED_EVENT_TYPES = frozenset(
    {
        "response.content_part.added",
        "response.content_part.done",
        "response.created",
        "response.function_call_arguments.delta",
        "response.in_progress",
        "response.output_item.done",
        "response.output_text.done",
        "response.queued",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
        "response.reasoning_summary_text.done",
    }
)


class OpenAIResponsesDecoder:
    def __init__(
        self,
        cost_microusd: Callable[[int, int, int, int], int],
    ) -> None:
        self._cost_microusd = cost_microusd
        self._provider_sequence: int | None = None
        self._canonical_sequence = 1
        self._event_count = 0
        self._function_calls: dict[str, tuple[str, str]] = {}
        self._tool_calls_emitted = 0
        self._terminal = False

    def decode(self, record: bytes) -> tuple[ProviderStreamEvent, ...]:
        try:
            event = self._parse(record)
            return self._decode_event(event)
        except OpenAIDecodeError:
            self._terminal = True
            raise
        except (OverflowError, TypeError, ValueError):
            self._terminal = True
            raise OpenAIDecodeError(
                OpenAIDecodeErrorCode.MALFORMED
            ) from None

    def _decode_event(
        self,
        event: dict[str, object],
    ) -> tuple[ProviderStreamEvent, ...]:
        event_type = string(event, "type", maximum=128)
        if event_type == "response.output_item.added":
            self._remember_function_call(event)
            return ()
        if event_type in _IGNORED_EVENT_TYPES:
            self._validate_ignored_event(event_type, event)
            return ()
        if event_type in {
            "response.output_text.delta",
            "response.refusal.delta",
        }:
            return (self._text_delta(event),)
        if event_type in {
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        }:
            return (self._reasoning_delta(event),)
        if event_type == "response.function_call_arguments.done":
            return (self._tool_call(event),)
        if event_type == "response.completed":
            return self._completed(event)
        if event_type == "response.incomplete":
            return self._incomplete(event)
        if event_type == "response.failed":
            return (self._failed(event),)
        if event_type == "error":
            return (self._error_event(event),)
        raise OpenAIDecodeError(OpenAIDecodeErrorCode.UNSUPPORTED)

    def cancel(self) -> ProviderCancelled:
        self._require_active()
        self._terminal = True
        return ProviderCancelled(
            sequence=self._next_sequence(),
            reason="Caller cancelled the provider stream.",
        )

    def _parse(self, record: bytes) -> dict[str, object]:
        self._require_active()
        if (
            not isinstance(record, bytes)
            or not 1 <= len(record) <= MAXIMUM_OPENAI_WIRE_EVENT_BYTES
        ):
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.EVENT_SIZE)
        self._event_count += 1
        if self._event_count > MAXIMUM_OPENAI_WIRE_EVENTS:
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.EVENT_LIMIT)
        try:
            event = json.loads(record)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED) from None
        if not isinstance(event, dict):
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED)
        provider_sequence = integer(event, "sequence_number")
        expected = (
            provider_sequence
            if self._provider_sequence is None
            else self._provider_sequence + 1
        )
        if provider_sequence < 0 or provider_sequence != expected:
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.SEQUENCE)
        self._provider_sequence = provider_sequence
        return cast(dict[str, object], event)

    def _remember_function_call(self, event: dict[str, object]) -> None:
        item = mapping(event, "item")
        item_type = string(item, "type", maximum=128)
        if item_type in {"message", "reasoning"}:
            return
        if item_type != "function_call":
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.UNSUPPORTED)
        item_id = string(item, "id", maximum=128)
        call_id = string(item, "call_id", maximum=128)
        name = string(item, "name", maximum=128)
        if (
            item_id in self._function_calls
            or len(self._function_calls) >= MAXIMUM_OPENAI_FUNCTION_CALLS
        ):
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED)
        self._function_calls[item_id] = (call_id, name)

    def _validate_ignored_event(
        self,
        event_type: str,
        event: dict[str, object],
    ) -> None:
        if event_type == "response.function_call_arguments.delta":
            item_id = string(event, "item_id", maximum=128)
            string(event, "delta", maximum=32 * 1024)
            if item_id not in self._function_calls:
                raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED)

    def _text_delta(self, event: dict[str, object]) -> ProviderTextDelta:
        return ProviderTextDelta(
            sequence=self._next_sequence(),
            text=string(event, "delta", maximum=32 * 1024),
        )

    def _reasoning_delta(
        self,
        event: dict[str, object],
    ) -> ProviderReasoningDelta:
        return ProviderReasoningDelta(
            sequence=self._next_sequence(),
            text=string(event, "delta", maximum=32 * 1024),
        )

    def _tool_call(self, event: dict[str, object]) -> ProviderToolCall:
        item_id = string(event, "item_id", maximum=128)
        expected = self._function_calls.pop(item_id, None)
        if expected is None:
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED)
        call_id, expected_name = expected
        name = string(event, "name", maximum=128)
        if name != expected_name:
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED)
        arguments = canonical_arguments(
            string(event, "arguments", maximum=128 * 1024)
        )
        self._tool_calls_emitted += 1
        return ProviderToolCall(
            sequence=self._next_sequence(),
            call_id=call_id,
            tool_name=name,
            arguments_json=arguments,
            arguments_sha256=hashlib.sha256(arguments.encode()).hexdigest(),
        )

    def _completed(
        self,
        event: dict[str, object],
    ) -> tuple[ProviderStreamEvent, ...]:
        response = mapping(event, "response")
        if response.get("status") != "completed":
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED)
        usage = self._usage(response)
        finish_reason = (
            ProviderFinishReason.TOOL_CALLS
            if self._tool_calls_emitted
            else ProviderFinishReason.STOP
        )
        completed = ProviderCompleted(
            sequence=self._next_sequence(),
            finish_reason=finish_reason,
        )
        self._terminal = True
        return (usage, completed)

    def _incomplete(
        self,
        event: dict[str, object],
    ) -> tuple[ProviderStreamEvent, ...]:
        response = mapping(event, "response")
        details = mapping(response, "incomplete_details")
        reason = string(details, "reason", maximum=128)
        if reason not in {"max_output_tokens", "max_tokens"}:
            self._terminal = True
            return (self._provider_error(reason),)
        usage = self._usage(response)
        completed = ProviderCompleted(
            sequence=self._next_sequence(),
            finish_reason=ProviderFinishReason.LENGTH,
        )
        self._terminal = True
        return (usage, completed)

    def _failed(self, event: dict[str, object]) -> ProviderError:
        response = mapping(event, "response")
        error = mapping(response, "error")
        self._terminal = True
        return self._provider_error(string(error, "code", maximum=128))

    def _error_event(self, event: dict[str, object]) -> ProviderError:
        self._terminal = True
        return self._provider_error(string(event, "code", maximum=128))

    def _usage(self, response: dict[str, object]) -> ProviderUsage:
        usage = mapping(response, "usage")
        input_tokens = integer(usage, "input_tokens")
        output_tokens = integer(usage, "output_tokens")
        cached_tokens = optional_nested_integer(
            usage, "input_tokens_details", "cached_tokens"
        )
        reasoning_tokens = optional_nested_integer(
            usage, "output_tokens_details", "reasoning_tokens"
        )
        cost = self._cost_microusd(
            input_tokens,
            cached_tokens,
            output_tokens,
            reasoning_tokens,
        )
        return ProviderUsage(
            sequence=self._next_sequence(),
            usage=ProviderTokenUsage(
                input_tokens=input_tokens,
                cached_input_tokens=cached_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                cost_microusd=cost,
            ),
        )

    def _provider_error(self, code: str) -> ProviderError:
        failure_class, retry_allowed = classify_error(code)
        return ProviderError(
            sequence=self._next_sequence(),
            failure_class=failure_class,
            retry_allowed=retry_allowed,
            reason="Provider returned a classified error.",
        )

    def _next_sequence(self) -> int:
        sequence = self._canonical_sequence
        self._canonical_sequence += 1
        return sequence

    def _require_active(self) -> None:
        if self._terminal:
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.TERMINAL)
