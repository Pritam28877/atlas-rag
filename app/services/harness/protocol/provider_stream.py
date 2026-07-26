"""Provider-neutral, bounded, and strictly discriminated stream records."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol.base import (
    BoundedReason,
    Sha256,
    StrictProtocolModel,
)
from app.services.harness.protocol.execution import ToolName

type ProviderCallId = Annotated[
    str,
    StringConstraints(
        min_length=8,
        max_length=128,
        pattern=r"^call_[0-9a-f]{3,123}$",
    ),
]
type ProviderDeltaText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=32 * 1024),
]


class ProviderStreamKind(StrEnum):
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_CALL = "tool_call"
    USAGE = "usage"
    ERROR = "error"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ProviderFailureClass(StrEnum):
    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    CONTEXT_LENGTH = "context_length"
    AUTHENTICATION = "authentication"
    POLICY = "policy"
    MALFORMED = "malformed"
    INTERNAL = "internal"


class ProviderFinishReason(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"


class ProviderTokenUsage(StrictProtocolModel):
    input_tokens: int = Field(ge=0, le=2_000_000)
    cached_input_tokens: int = Field(ge=0, le=2_000_000)
    output_tokens: int = Field(ge=0, le=512_000)
    reasoning_tokens: int = Field(ge=0, le=512_000)
    cost_microusd: int = Field(ge=0, le=10_000_000_000)

    @model_validator(mode="after")
    def validate_cached_tokens(self) -> Self:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens cannot exceed input tokens")
        return self


class ProviderTextDelta(StrictProtocolModel):
    kind: Literal[ProviderStreamKind.TEXT_DELTA] = ProviderStreamKind.TEXT_DELTA
    sequence: int = Field(ge=1, le=1_000_000)
    text: ProviderDeltaText


class ProviderReasoningDelta(StrictProtocolModel):
    kind: Literal[ProviderStreamKind.REASONING_DELTA] = (
        ProviderStreamKind.REASONING_DELTA
    )
    sequence: int = Field(ge=1, le=1_000_000)
    text: ProviderDeltaText


class ProviderToolCall(StrictProtocolModel):
    kind: Literal[ProviderStreamKind.TOOL_CALL] = ProviderStreamKind.TOOL_CALL
    sequence: int = Field(ge=1, le=1_000_000)
    call_id: ProviderCallId
    tool_name: ToolName
    arguments_json: str = Field(min_length=2, max_length=128 * 1024)
    arguments_sha256: Sha256

    @model_validator(mode="after")
    def validate_arguments(self) -> Self:
        try:
            arguments = json.loads(self.arguments_json)
        except json.JSONDecodeError as error:
            raise ValueError("tool arguments must be valid JSON") from error
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be a JSON object")
        canonical = json.dumps(
            arguments,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if canonical != self.arguments_json:
            raise ValueError("tool arguments must use canonical JSON")
        actual_digest = hashlib.sha256(self.arguments_json.encode()).hexdigest()
        if actual_digest != self.arguments_sha256:
            raise ValueError("tool arguments hash mismatch")
        return self


class ProviderUsage(StrictProtocolModel):
    kind: Literal[ProviderStreamKind.USAGE] = ProviderStreamKind.USAGE
    sequence: int = Field(ge=1, le=1_000_000)
    usage: ProviderTokenUsage


class ProviderError(StrictProtocolModel):
    kind: Literal[ProviderStreamKind.ERROR] = ProviderStreamKind.ERROR
    sequence: int = Field(ge=1, le=1_000_000)
    failure_class: ProviderFailureClass
    retry_allowed: bool
    reason: BoundedReason

    @model_validator(mode="after")
    def validate_retry_classification(self) -> Self:
        retryable = self.failure_class in {
            ProviderFailureClass.TRANSIENT,
            ProviderFailureClass.RATE_LIMIT,
        }
        if self.retry_allowed != retryable:
            raise ValueError("provider retry classification is inconsistent")
        return self


class ProviderCompleted(StrictProtocolModel):
    kind: Literal[ProviderStreamKind.COMPLETED] = ProviderStreamKind.COMPLETED
    sequence: int = Field(ge=1, le=1_000_000)
    finish_reason: ProviderFinishReason


class ProviderCancelled(StrictProtocolModel):
    kind: Literal[ProviderStreamKind.CANCELLED] = ProviderStreamKind.CANCELLED
    sequence: int = Field(ge=1, le=1_000_000)
    reason: BoundedReason


type ProviderStreamEvent = Annotated[
    ProviderTextDelta
    | ProviderReasoningDelta
    | ProviderToolCall
    | ProviderUsage
    | ProviderError
    | ProviderCompleted
    | ProviderCancelled,
    Field(discriminator="kind"),
]


class ProviderStreamBatch(StrictProtocolModel):
    events: tuple[ProviderStreamEvent, ...] = Field(
        min_length=1,
        max_length=256,
    )

    @model_validator(mode="after")
    def validate_contiguous_sequence(self) -> Self:
        first_sequence = self.events[0].sequence
        expected = tuple(
            range(first_sequence, first_sequence + len(self.events))
        )
        actual = tuple(event.sequence for event in self.events)
        if actual != expected:
            raise ValueError("provider stream batch sequence must be contiguous")
        return self
