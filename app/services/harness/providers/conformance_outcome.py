"""Bounded semantic projection of canonical provider stream events."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderCompleted,
    ProviderError,
    ProviderFailureClass,
    ProviderFinishReason,
    ProviderReasoningDelta,
    ProviderStreamEvent,
    ProviderStreamKind,
    ProviderTextDelta,
    ProviderTokenUsage,
    ProviderToolCall,
    ProviderUsage,
)
from app.services.harness.providers.conformance_contracts import (
    MAXIMUM_CONFORMANCE_EVENTS,
    ConformanceOutcome,
    ConformanceToolSignature,
)


class _Digest(Protocol):
    def update(self, value: bytes) -> None: ...

    def hexdigest(self) -> str: ...


class ConformanceOutcomeAccumulator:
    def __init__(self) -> None:
        self._event_kinds: list[ProviderStreamKind] = []
        self._expected_sequence = 1
        self._terminal = False
        self._text = hashlib.sha256()
        self._text_seen = False
        self._reasoning = hashlib.sha256()
        self._reasoning_seen = False
        self._tools: list[ConformanceToolSignature] = []
        self._usage: ProviderTokenUsage | None = None
        self._failure_class: ProviderFailureClass | None = None
        self._retry_allowed: bool | None = None
        self._finish_reason: ProviderFinishReason | None = None
        self._cancelled = False

    def consume(self, event: ProviderStreamEvent) -> None:
        if (
            self._terminal
            or event.sequence != self._expected_sequence
            or len(self._event_kinds) >= MAXIMUM_CONFORMANCE_EVENTS
        ):
            raise ValueError("conformance event stream is invalid")
        self._expected_sequence += 1
        self._event_kinds.append(event.kind)
        if isinstance(event, ProviderTextDelta):
            _hash_part(self._text, event.text)
            self._text_seen = True
        elif isinstance(event, ProviderReasoningDelta):
            _hash_part(self._reasoning, event.text)
            self._reasoning_seen = True
        elif isinstance(event, ProviderToolCall):
            self._tools.append(
                ConformanceToolSignature(
                    tool_name=event.tool_name,
                    arguments_sha256=event.arguments_sha256,
                )
            )
        elif isinstance(event, ProviderUsage):
            if self._usage is not None:
                raise ValueError("conformance usage is duplicated")
            self._usage = event.usage
        elif isinstance(event, ProviderError):
            self._failure_class = event.failure_class
            self._retry_allowed = event.retry_allowed
            self._terminal = True
        elif isinstance(event, ProviderCompleted):
            self._finish_reason = event.finish_reason
            self._terminal = True
        elif isinstance(event, ProviderCancelled):
            self._cancelled = True
            self._terminal = True

    def finish(self) -> ConformanceOutcome:
        if not self._terminal:
            raise ValueError("conformance stream has no terminal event")
        text_sha256 = (
            self._text.hexdigest() if self._text_seen else None
        )
        reasoning_sha256 = (
            self._reasoning.hexdigest()
            if self._reasoning_seen
            else None
        )
        semantic = {
            "cancelled": self._cancelled,
            "event_kinds": _normalized_kinds(
                self._event_kinds,
            ),
            "failure_class": self._failure_class,
            "finish_reason": self._finish_reason,
            "reasoning_sha256": reasoning_sha256,
            "retry_allowed": self._retry_allowed,
            "text_sha256": text_sha256,
            "tools": [
                tool.model_dump(mode="json") for tool in self._tools
            ],
            "usage_observed": (
                self._usage is not None
                and self._event_kinds[-1]
                is ProviderStreamKind.COMPLETED
            ),
        }
        encoded = json.dumps(
            semantic,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return ConformanceOutcome(
            event_kinds=tuple(self._event_kinds),
            text_sha256=text_sha256,
            reasoning_sha256=reasoning_sha256,
            tools=tuple(self._tools),
            usage=self._usage,
            failure_class=self._failure_class,
            retry_allowed=self._retry_allowed,
            finish_reason=self._finish_reason,
            cancelled=self._cancelled,
            equivalence_sha256=hashlib.sha256(encoded).hexdigest(),
        )


def _hash_part(digest: _Digest, value: str) -> None:
    digest.update(value.encode())


def _normalized_kinds(
    event_kinds: list[ProviderStreamKind],
) -> tuple[ProviderStreamKind, ...]:
    normalized: list[ProviderStreamKind] = []
    delta_kinds = {
        ProviderStreamKind.REASONING_DELTA,
        ProviderStreamKind.TEXT_DELTA,
    }
    terminal_ignores_usage = event_kinds[-1] in {
        ProviderStreamKind.CANCELLED,
        ProviderStreamKind.ERROR,
    }
    for kind in event_kinds:
        if terminal_ignores_usage and kind is ProviderStreamKind.USAGE:
            continue
        if kind not in delta_kinds or not normalized or normalized[-1] != kind:
            normalized.append(kind)
    return tuple(normalized)
