"""Normalize documented OpenRouter beta events into Responses events."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderStreamEvent,
)
from app.services.harness.providers.openai_decode_support import (
    MAXIMUM_OPENAI_WIRE_EVENT_BYTES,
    MAXIMUM_OPENAI_WIRE_EVENTS,
    OpenAIDecodeError,
    OpenAIDecodeErrorCode,
)
from app.services.harness.providers.openai_decoder import (
    OpenAIResponsesDecoder,
)
from app.services.harness.providers.openrouter_contracts import (
    OpenRouterRoutingMetadata,
    compile_routing_metadata,
)

_TERMINAL_TYPES = frozenset(
    {
        "error",
        "response.completed",
        "response.done",
        "response.failed",
        "response.incomplete",
    }
)


class OpenRouterResponsesDecoder:
    def __init__(
        self,
        cost_microusd: Callable[[int, int, int, int], int],
    ) -> None:
        self._compatible = OpenAIResponsesDecoder(cost_microusd)
        self._sequence = 0
        self._record_count = 0
        self._terminal = False
        self._routing_metadata: OpenRouterRoutingMetadata | None = None

    @property
    def routing_metadata(self) -> OpenRouterRoutingMetadata | None:
        return self._routing_metadata

    def decode(self, record: bytes) -> tuple[ProviderStreamEvent, ...]:
        if (
            not isinstance(record, bytes)
            or not 1 <= len(record) <= MAXIMUM_OPENAI_WIRE_EVENT_BYTES
        ):
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.EVENT_SIZE)
        self._record_count += 1
        if self._record_count > MAXIMUM_OPENAI_WIRE_EVENTS:
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.EVENT_LIMIT)
        stripped = record.strip()
        if stripped.startswith(b":"):
            return ()
        if stripped in {b"[DONE]", b"data: [DONE]"}:
            if not self._terminal:
                raise OpenAIDecodeError(OpenAIDecodeErrorCode.SEQUENCE)
            return ()
        if self._terminal:
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.TERMINAL)
        event = _parse_record(stripped)
        self._capture_metadata(event)
        event_type = _normalize_type(event)
        self._sequence += 1
        event["sequence_number"] = self._sequence
        try:
            normalized = json.dumps(
                event,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        except (TypeError, ValueError):
            self._terminal = True
            raise OpenAIDecodeError(
                OpenAIDecodeErrorCode.MALFORMED
            ) from None
        events = self._compatible.decode(normalized)
        if event_type in _TERMINAL_TYPES:
            self._terminal = True
        return events

    def cancel(self) -> ProviderCancelled:
        if self._terminal:
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.TERMINAL)
        self._terminal = True
        return self._compatible.cancel()

    def _capture_metadata(self, event: dict[str, object]) -> None:
        response = event.get("response")
        metadata: object | None = event.get("openrouter_metadata")
        if isinstance(response, dict):
            response_values = cast(dict[str, object], response)
            metadata = response_values.get(
                "openrouter_metadata",
                metadata,
            )
        if metadata is None:
            return
        if not isinstance(metadata, dict):
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED)
        try:
            compiled = compile_routing_metadata(
                cast(dict[str, object], metadata)
            )
        except ValueError:
            raise OpenAIDecodeError(
                OpenAIDecodeErrorCode.MALFORMED
            ) from None
        if (
            self._routing_metadata is not None
            and self._routing_metadata != compiled
        ):
            raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED)
        self._routing_metadata = compiled


def _parse_record(record: bytes) -> dict[str, object]:
    if record.startswith(b"data:"):
        record = record[5:].strip()
    try:
        event = json.loads(record)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED) from None
    if not isinstance(event, dict):
        raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED)
    return cast(dict[str, object], event)


def _normalize_type(event: dict[str, object]) -> str:
    event_type = event.get("type")
    if event_type == "response.content_part.delta":
        event["type"] = "response.output_text.delta"
        return "response.output_text.delta"
    if event_type == "response.done":
        event["type"] = "response.completed"
        return "response.done"
    if isinstance(event_type, str):
        return event_type
    error = event.get("error")
    if not isinstance(error, dict):
        raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED)
    error_values = cast(dict[str, object], error)
    code = error_values.get("error_type", error_values.get("code"))
    if not isinstance(code, str):
        raise OpenAIDecodeError(OpenAIDecodeErrorCode.MALFORMED)
    event["type"] = "error"
    event["code"] = code
    return "error"
