"""Provider-neutral attempt orchestration contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pydantic import Field

from app.services.harness.protocol import (
    ProviderCostLimits,
    ProviderRetryBudget,
    RequestId,
    Sha256,
    StrictProtocolModel,
    TurnId,
    UtcTimestamp,
    WorkspaceId,
)


class ProviderDispatchRequest(StrictProtocolModel):
    workspace_id: WorkspaceId
    turn_id: TurnId
    request_id: RequestId
    provider_request_sha256: Sha256
    estimated_attempt_cost_microusd: int = Field(
        ge=0,
        le=10_000_000_000,
    )
    cost_limits: ProviderCostLimits
    retry_budget: ProviderRetryBudget
    deadline_at: UtcTimestamp


@dataclass(frozen=True, slots=True)
class ProviderAttemptSuccess[ResultT]:
    value: ResultT
    actual_cost_microusd: int

    def __post_init__(self) -> None:
        if not 0 <= self.actual_cost_microusd <= 10_000_000_000:
            raise ValueError("provider attempt actual cost is invalid")


class ProviderAttemptExecutor[ResultT](Protocol):
    async def execute(
        self,
        attempt: int,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> ProviderAttemptSuccess[ResultT]: ...


class ProviderRetryDelay(Protocol):
    async def wait(
        self,
        delay_ms: int,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> None: ...
