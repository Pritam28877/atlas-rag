"""Bounded provider retry budgets and deterministic decisions."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedReason,
    StrictProtocolModel,
    UtcTimestamp,
)


class ProviderRetryBudget(StrictProtocolModel):
    max_attempts: int = Field(ge=1, le=6)
    max_wall_time_ms: int = Field(ge=100, le=3_600_000)
    base_delay_ms: int = Field(ge=1, le=60_000)
    max_delay_ms: int = Field(ge=1, le=3_600_000)

    @model_validator(mode="after")
    def validate_delay_bounds(self) -> Self:
        if self.base_delay_ms > self.max_delay_ms:
            raise ValueError("provider retry delay bounds are reversed")
        return self


class ProviderRetryDecisionCode(StrEnum):
    AMBIGUOUS = "ambiguous"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    RETRY = "retry"
    RETRY_PROHIBITED = "retry_prohibited"
    WALL_TIME_EXHAUSTED = "wall_time_exhausted"


class ProviderRetryDecision(StrictProtocolModel):
    retry: bool
    code: ProviderRetryDecisionCode
    reason: BoundedReason
    current_attempt: int = Field(ge=1, le=64)
    next_attempt: int | None = Field(default=None, ge=2, le=64)
    delay_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    decided_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        has_retry_evidence = (
            self.next_attempt is not None and self.delay_ms is not None
        )
        if self.retry != has_retry_evidence:
            raise ValueError("provider retry decision evidence is inconsistent")
        if self.retry != (self.code is ProviderRetryDecisionCode.RETRY):
            raise ValueError("provider retry decision code is inconsistent")
        if (
            self.next_attempt is not None
            and self.next_attempt != self.current_attempt + 1
        ):
            raise ValueError("provider retry attempt must advance by one")
        return self
