"""Strict bounded primitives shared by every Atlas Harness protocol record."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


def _identifier_constraints(prefix: str) -> StringConstraints:
    return StringConstraints(
        min_length=36,
        max_length=36,
        pattern=rf"^{prefix}_[0-9a-f]{{32}}$",
    )


type PrincipalId = Annotated[str, _identifier_constraints("prn")]
type TenantId = Annotated[str, _identifier_constraints("ten")]
type GrantId = Annotated[str, _identifier_constraints("grt")]
type WorkspaceId = Annotated[str, _identifier_constraints("wsp")]
type ThreadId = Annotated[str, _identifier_constraints("thr")]
type TurnId = Annotated[str, _identifier_constraints("trn")]
type ItemId = Annotated[str, _identifier_constraints("itm")]
type EventId = Annotated[str, _identifier_constraints("evt")]
type OperationId = Annotated[str, _identifier_constraints("opn")]
type ApprovalId = Annotated[str, _identifier_constraints("apr")]
type TaskId = Annotated[str, _identifier_constraints("tsk")]
type TaskGraphId = Annotated[str, _identifier_constraints("tgr")]
type ArtifactId = Annotated[str, _identifier_constraints("art")]
type ContextId = Annotated[str, _identifier_constraints("ctx")]
type ProviderDecisionId = Annotated[
    str,
    _identifier_constraints("pvd"),
]
type EvaluationId = Annotated[str, _identifier_constraints("evl")]
type DecisionId = Annotated[str, _identifier_constraints("dcs")]
type RequestId = Annotated[str, _identifier_constraints("req")]
type ClientId = Annotated[str, _identifier_constraints("cli")]
type SubscriptionId = Annotated[str, _identifier_constraints("sub")]
type AggregateId = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^(?:wsp|thr|trn|opn|apr|tsk|art|pvd|evl)_"
            r"[0-9a-f]{32}$"
        )
    ),
]

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SchemaVersion = Annotated[
    str,
    StringConstraints(pattern=r"^1\.[0-9]{1,3}$", max_length=5),
]
PolicyVersion = Annotated[
    str,
    StringConstraints(pattern=r"^pol_[0-9a-f]{64}$", max_length=68),
]
IdempotencyKey = Annotated[
    str,
    StringConstraints(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    ),
]
Cursor = Annotated[
    str,
    StringConstraints(
        min_length=16,
        max_length=512,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]
Capability = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$",
    ),
]
MediaType = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=127,
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$",
    ),
]
BoundedLabel = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$"),
]
BoundedReason = Annotated[
    str,
    StringConstraints(min_length=1, max_length=2048, pattern=r"^[^\x00]+$"),
]
InlineText = Annotated[str, StringConstraints(min_length=1, max_length=32768)]


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must include an explicit UTC offset")
    return value


UtcTimestamp = Annotated[datetime, AfterValidator(_require_utc)]


class StrictProtocolModel(BaseModel):
    """Immutable model that rejects unknown fields and Python type coercion."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class TraceLink(StrictProtocolModel):
    """Request, correlation, and causation link for an accepted operation."""

    request_id: RequestId
    correlation_id: EventId
    causation_event_id: EventId | None = None


class PageRequest(StrictProtocolModel):
    """Bounded cursor pagination input."""

    cursor: Cursor | None = None
    limit: int = Field(default=50, ge=1, le=200)


class PageInfo(StrictProtocolModel):
    """Pagination result metadata without an unbounded total count."""

    next_cursor: Cursor | None = None
    has_more: bool

    @model_validator(mode="after")
    def cursor_matches_has_more(self) -> Self:
        if self.has_more != (self.next_cursor is not None):
            message = "next_cursor must be present exactly when has_more is true"
            raise ValueError(message)
        return self


class ExecutionBudget(StrictProtocolModel):
    """Hard ceilings owned by a turn, task, or external operation."""

    max_steps: int = Field(ge=1, le=256)
    max_tool_calls: int = Field(ge=0, le=256)
    max_input_tokens: int = Field(ge=1, le=2_000_000)
    max_output_tokens: int = Field(ge=1, le=512_000)
    max_tool_output_bytes: int = Field(ge=0, le=16 * 1024 * 1024)
    max_duration_ms: int = Field(ge=100, le=3_600_000)
    max_cost_microusd: int = Field(ge=0, le=10_000_000_000)


class ResourceUsage(StrictProtocolModel):
    """Monotonic consumed resources recorded without floating-point currency."""

    steps: int = Field(ge=0, le=256)
    tool_calls: int = Field(ge=0, le=256)
    input_tokens: int = Field(ge=0, le=2_000_000)
    output_tokens: int = Field(ge=0, le=512_000)
    tool_output_bytes: int = Field(ge=0, le=16 * 1024 * 1024)
    duration_ms: int = Field(ge=0, le=3_600_000)
    cost_microusd: int = Field(ge=0, le=10_000_000_000)

    def exceeds(self, budget: ExecutionBudget) -> tuple[str, ...]:
        exceeded: list[str] = []
        comparisons = (
            ("steps", self.steps, budget.max_steps),
            ("tool_calls", self.tool_calls, budget.max_tool_calls),
            ("input_tokens", self.input_tokens, budget.max_input_tokens),
            ("output_tokens", self.output_tokens, budget.max_output_tokens),
            (
                "tool_output_bytes",
                self.tool_output_bytes,
                budget.max_tool_output_bytes,
            ),
            ("duration_ms", self.duration_ms, budget.max_duration_ms),
            ("cost_microusd", self.cost_microusd, budget.max_cost_microusd),
        )
        for name, consumed, maximum in comparisons:
            if consumed > maximum:
                exceeded.append(name)
        return tuple(exceeded)


class InlinePayload(StrictProtocolModel):
    """Small UTF-8 payload whose declared hash and size are independently checked."""

    kind: Literal["inline_text"] = "inline_text"
    media_type: MediaType = "text/plain"
    text: InlineText
    size_bytes: int = Field(ge=1, le=128 * 1024)
    content_sha256: Sha256

    @model_validator(mode="after")
    def content_matches_metadata(self) -> Self:
        encoded = self.text.encode("utf-8")
        if len(encoded) != self.size_bytes:
            raise ValueError("inline payload size does not match UTF-8 content")
        actual_digest = hashlib.sha256(encoded).hexdigest()
        if actual_digest != self.content_sha256:
            raise ValueError("inline payload hash does not match content")
        return self


class BlobPayload(StrictProtocolModel):
    """Content-addressed reference for payloads that do not belong in events."""

    kind: Literal["blob"] = "blob"
    artifact_id: ArtifactId
    media_type: MediaType
    size_bytes: int = Field(ge=1, le=4 * 1024 * 1024 * 1024)
    content_sha256: Sha256


Payload = Annotated[InlinePayload | BlobPayload, Field(discriminator="kind")]
