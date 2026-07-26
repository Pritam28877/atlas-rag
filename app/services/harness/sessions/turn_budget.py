"""Immutable aggregate budget accounting that survives turn resumes."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    ExecutionBudget,
    ResourceUsage,
    Sha256,
    StrictProtocolModel,
    TurnId,
)

MAXIMUM_TRACKED_CALLS = 256


class TurnBudgetDimension(StrEnum):
    STEPS = "steps"
    TOOL_CALLS = "tool_calls"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    TOOL_OUTPUT_BYTES = "tool_output_bytes"
    DURATION_MS = "duration_ms"
    COST_MICROUSD = "cost_microusd"
    PROVIDER_RETRIES = "provider_retries"
    IDENTICAL_CALLS = "identical_calls"
    STREAM_BYTES = "stream_bytes"


class TurnLoopLimits(StrictProtocolModel):
    max_provider_retries: int = Field(ge=0, le=5)
    max_identical_calls: int = Field(ge=1, le=5)
    max_stream_bytes: int = Field(ge=1, le=16 * 1024 * 1024)


class RepeatedCallCounter(StrictProtocolModel):
    call_sha256: Sha256
    count: int = Field(ge=1, le=5)


class TurnBudgetSnapshot(StrictProtocolModel):
    turn_id: TurnId
    budget: ExecutionBudget
    loop_limits: TurnLoopLimits
    usage: ResourceUsage
    provider_retries: int = Field(ge=0, le=5)
    stream_bytes: int = Field(ge=0, le=16 * 1024 * 1024)
    repeated_calls: tuple[RepeatedCallCounter, ...] = Field(
        max_length=MAXIMUM_TRACKED_CALLS
    )
    revision: int = Field(ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def validate_aggregate_limits(self) -> Self:
        exceeded = self.usage.exceeds(self.budget)
        if exceeded:
            raise ValueError("turn budget snapshot exceeds execution budget")
        if self.provider_retries > self.loop_limits.max_provider_retries:
            raise ValueError("turn budget snapshot exceeds provider retries")
        if self.stream_bytes > self.loop_limits.max_stream_bytes:
            raise ValueError("turn budget snapshot exceeds stream bytes")
        digests = tuple(counter.call_sha256 for counter in self.repeated_calls)
        if tuple(sorted(set(digests))) != digests:
            raise ValueError("repeated call counters must be unique and sorted")
        if any(
            counter.count > self.loop_limits.max_identical_calls
            for counter in self.repeated_calls
        ):
            raise ValueError("turn budget snapshot exceeds identical calls")
        return self


class TurnBudgetCharge(StrictProtocolModel):
    steps: int = Field(default=0, ge=0, le=256)
    tool_calls: int = Field(default=0, ge=0, le=1)
    input_tokens: int = Field(default=0, ge=0, le=2_000_000)
    output_tokens: int = Field(default=0, ge=0, le=512_000)
    tool_output_bytes: int = Field(
        default=0,
        ge=0,
        le=16 * 1024 * 1024,
    )
    duration_ms: int = Field(default=0, ge=0, le=3_600_000)
    cost_microusd: int = Field(default=0, ge=0, le=10_000_000_000)
    provider_retries: int = Field(default=0, ge=0, le=1)
    stream_bytes: int = Field(default=0, ge=0, le=16 * 1024 * 1024)
    call_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_charge(self) -> Self:
        numeric_values = (
            self.steps,
            self.tool_calls,
            self.input_tokens,
            self.output_tokens,
            self.tool_output_bytes,
            self.duration_ms,
            self.cost_microusd,
            self.provider_retries,
            self.stream_bytes,
        )
        if not any(numeric_values):
            raise ValueError("turn budget charge must consume a resource")
        if (self.call_sha256 is not None) != (self.tool_calls == 1):
            raise ValueError("tool call charge requires exactly one call digest")
        return self


class TurnBudgetExceeded(RuntimeError):
    def __init__(self, dimensions: tuple[TurnBudgetDimension, ...]) -> None:
        super().__init__("turn budget charge exceeds limits")
        self.dimensions = dimensions


def initial_turn_budget(
    turn_id: TurnId,
    budget: ExecutionBudget,
    loop_limits: TurnLoopLimits,
) -> TurnBudgetSnapshot:
    return TurnBudgetSnapshot(
        turn_id=turn_id,
        budget=budget,
        loop_limits=loop_limits,
        usage=ResourceUsage(
            steps=0,
            tool_calls=0,
            input_tokens=0,
            output_tokens=0,
            tool_output_bytes=0,
            duration_ms=0,
            cost_microusd=0,
        ),
        provider_retries=0,
        stream_bytes=0,
        repeated_calls=(),
        revision=0,
    )


def charge_turn_budget(
    snapshot: TurnBudgetSnapshot,
    charge: TurnBudgetCharge,
) -> TurnBudgetSnapshot:
    usage_values = {
        "steps": snapshot.usage.steps + charge.steps,
        "tool_calls": snapshot.usage.tool_calls + charge.tool_calls,
        "input_tokens": snapshot.usage.input_tokens + charge.input_tokens,
        "output_tokens": snapshot.usage.output_tokens + charge.output_tokens,
        "tool_output_bytes": (
            snapshot.usage.tool_output_bytes + charge.tool_output_bytes
        ),
        "duration_ms": snapshot.usage.duration_ms + charge.duration_ms,
        "cost_microusd": snapshot.usage.cost_microusd + charge.cost_microusd,
    }
    provider_retries = snapshot.provider_retries + charge.provider_retries
    stream_bytes = snapshot.stream_bytes + charge.stream_bytes
    repeated_calls, identical_call_count = _charge_repeated_call(
        snapshot.repeated_calls,
        charge.call_sha256,
    )
    exceeded = _exceeded_dimensions(
        snapshot,
        usage_values,
        provider_retries,
        stream_bytes,
        identical_call_count,
    )
    if exceeded:
        raise TurnBudgetExceeded(exceeded)
    return TurnBudgetSnapshot(
        turn_id=snapshot.turn_id,
        budget=snapshot.budget,
        loop_limits=snapshot.loop_limits,
        usage=ResourceUsage(**usage_values),
        provider_retries=provider_retries,
        stream_bytes=stream_bytes,
        repeated_calls=repeated_calls,
        revision=snapshot.revision + 1,
    )


def _charge_repeated_call(
    counters: tuple[RepeatedCallCounter, ...],
    call_sha256: str | None,
) -> tuple[tuple[RepeatedCallCounter, ...], int]:
    if call_sha256 is None:
        return counters, 0
    updated: list[RepeatedCallCounter] = []
    matched = False
    identical_count = 1
    for counter in counters:
        if counter.call_sha256 == call_sha256:
            identical_count = counter.count + 1
            if identical_count > 5:
                raise TurnBudgetExceeded(
                    (TurnBudgetDimension.IDENTICAL_CALLS,)
                )
            updated.append(
                RepeatedCallCounter(
                    call_sha256=call_sha256,
                    count=identical_count,
                )
            )
            matched = True
        else:
            updated.append(counter)
    if not matched:
        if len(updated) >= MAXIMUM_TRACKED_CALLS:
            raise TurnBudgetExceeded(
                (TurnBudgetDimension.TOOL_CALLS,)
            )
        updated.append(RepeatedCallCounter(call_sha256=call_sha256, count=1))
    updated.sort(key=lambda counter: counter.call_sha256)
    return tuple(updated), identical_count


def _exceeded_dimensions(
    snapshot: TurnBudgetSnapshot,
    usage: dict[str, int],
    provider_retries: int,
    stream_bytes: int,
    identical_call_count: int,
) -> tuple[TurnBudgetDimension, ...]:
    comparisons = (
        (TurnBudgetDimension.STEPS, usage["steps"], snapshot.budget.max_steps),
        (
            TurnBudgetDimension.TOOL_CALLS,
            usage["tool_calls"],
            snapshot.budget.max_tool_calls,
        ),
        (
            TurnBudgetDimension.INPUT_TOKENS,
            usage["input_tokens"],
            snapshot.budget.max_input_tokens,
        ),
        (
            TurnBudgetDimension.OUTPUT_TOKENS,
            usage["output_tokens"],
            snapshot.budget.max_output_tokens,
        ),
        (
            TurnBudgetDimension.TOOL_OUTPUT_BYTES,
            usage["tool_output_bytes"],
            snapshot.budget.max_tool_output_bytes,
        ),
        (
            TurnBudgetDimension.DURATION_MS,
            usage["duration_ms"],
            snapshot.budget.max_duration_ms,
        ),
        (
            TurnBudgetDimension.COST_MICROUSD,
            usage["cost_microusd"],
            snapshot.budget.max_cost_microusd,
        ),
        (
            TurnBudgetDimension.PROVIDER_RETRIES,
            provider_retries,
            snapshot.loop_limits.max_provider_retries,
        ),
        (
            TurnBudgetDimension.IDENTICAL_CALLS,
            identical_call_count,
            snapshot.loop_limits.max_identical_calls,
        ),
        (
            TurnBudgetDimension.STREAM_BYTES,
            stream_bytes,
            snapshot.loop_limits.max_stream_bytes,
        ),
    )
    return tuple(
        dimension
        for dimension, consumed, maximum in comparisons
        if consumed > maximum
    )
