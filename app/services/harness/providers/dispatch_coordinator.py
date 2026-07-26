"""Cost-reserved bounded provider attempt coordination."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

from app.services.harness.protocol import (
    ProviderCostLedger,
    ProviderCostReleaseRequest,
    ProviderCostReservationRequest,
    ProviderCostSettlementRequest,
    ProviderRetryDecisionCode,
    ProviderTransportError,
)
from app.services.harness.providers.dispatch_contracts import (
    ProviderAttemptExecutor,
    ProviderDispatchRequest,
    ProviderRetryDelay,
)
from app.services.harness.providers.retry_planner import plan_provider_retry


class ProviderDispatchErrorCode(StrEnum):
    AMBIGUOUS = "ambiguous"
    CANCELLED = "cancelled"
    COST = "cost"
    DEADLINE = "deadline"
    RECONCILIATION = "reconciliation"
    TRANSPORT = "transport"


class ProviderDispatchError(RuntimeError):
    def __init__(self, code: ProviderDispatchErrorCode) -> None:
        super().__init__("provider dispatch rejected the operation")
        self.code = code


class ProviderDispatchCoordinator:
    def __init__(
        self,
        cost_ledger: ProviderCostLedger,
        retry_delay: ProviderRetryDelay,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._cost_ledger = cost_ledger
        self._retry_delay = retry_delay
        self._clock = clock

    async def dispatch[ResultT](
        self,
        request: ProviderDispatchRequest,
        executor: ProviderAttemptExecutor[ResultT],
        *,
        cancellation: asyncio.Event,
    ) -> ResultT:
        started_at = self._clock()
        attempt = 1
        while True:
            self._check_runtime(
                cancellation,
                request.deadline_at,
            )
            reservation = ProviderCostReservationRequest(
                reservation_id=_reservation_id(request, attempt),
                workspace_id=request.workspace_id,
                turn_id=request.turn_id,
                request_id=request.request_id,
                provider_request_sha256=request.provider_request_sha256,
                attempt=attempt,
                estimated_cost_microusd=(
                    request.estimated_attempt_cost_microusd
                ),
                limits=request.cost_limits,
                requested_at=self._clock(),
            )
            try:
                decision = await self._cost_ledger.reserve(reservation)
            except Exception:
                raise ProviderDispatchError(
                    ProviderDispatchErrorCode.COST
                ) from None
            if not decision.allowed:
                raise ProviderDispatchError(ProviderDispatchErrorCode.COST)
            if decision.already_exists:
                raise ProviderDispatchError(
                    ProviderDispatchErrorCode.AMBIGUOUS
                )
            if cancellation.is_set():
                await self._release(reservation)
                raise ProviderDispatchError(
                    ProviderDispatchErrorCode.CANCELLED
                )
            try:
                success = await executor.execute(
                    attempt,
                    cancellation=cancellation,
                    deadline_at=request.deadline_at,
                )
            except ProviderTransportError as error:
                await self._settle(
                    reservation,
                    reservation.estimated_cost_microusd,
                )
                if (
                    error.failure.provider_request_sha256
                    != request.provider_request_sha256
                ):
                    raise ProviderDispatchError(
                        ProviderDispatchErrorCode.AMBIGUOUS
                    ) from None
                decided_at = self._clock()
                retry = plan_provider_retry(
                    error.failure,
                    request.retry_budget,
                    current_attempt=attempt,
                    started_at=started_at,
                    deadline_at=request.deadline_at,
                    decided_at=decided_at,
                )
                if not retry.retry:
                    code = (
                        ProviderDispatchErrorCode.AMBIGUOUS
                        if retry.code
                        is ProviderRetryDecisionCode.AMBIGUOUS
                        else ProviderDispatchErrorCode.TRANSPORT
                    )
                    raise ProviderDispatchError(code) from None
                try:
                    await self._retry_delay.wait(
                        retry.delay_ms or 0,
                        cancellation=cancellation,
                        deadline_at=request.deadline_at,
                    )
                except Exception:
                    raise ProviderDispatchError(
                        ProviderDispatchErrorCode.CANCELLED
                    ) from None
                attempt = retry.next_attempt or (attempt + 1)
                continue
            except asyncio.CancelledError:
                await self._settle(
                    reservation,
                    reservation.estimated_cost_microusd,
                )
                raise
            except Exception:
                await self._settle(
                    reservation,
                    reservation.estimated_cost_microusd,
                )
                raise ProviderDispatchError(
                    ProviderDispatchErrorCode.AMBIGUOUS
                ) from None
            await self._settle(
                reservation,
                success.actual_cost_microusd,
            )
            return success.value

    async def _settle(
        self,
        reservation: ProviderCostReservationRequest,
        actual_cost_microusd: int,
    ) -> None:
        try:
            await self._cost_ledger.settle(
                ProviderCostSettlementRequest(
                    reservation_id=reservation.reservation_id,
                    provider_request_sha256=(
                        reservation.provider_request_sha256
                    ),
                    actual_cost_microusd=actual_cost_microusd,
                    settled_at=self._clock(),
                )
            )
        except Exception:
            raise ProviderDispatchError(
                ProviderDispatchErrorCode.RECONCILIATION
            ) from None

    async def _release(
        self,
        reservation: ProviderCostReservationRequest,
    ) -> None:
        try:
            await self._cost_ledger.release(
                ProviderCostReleaseRequest(
                    reservation_id=reservation.reservation_id,
                    provider_request_sha256=(
                        reservation.provider_request_sha256
                    ),
                    released_at=self._clock(),
                )
            )
        except Exception:
            raise ProviderDispatchError(
                ProviderDispatchErrorCode.RECONCILIATION
            ) from None

    def _check_runtime(
        self,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> None:
        if cancellation.is_set():
            raise ProviderDispatchError(
                ProviderDispatchErrorCode.CANCELLED
            )
        if self._clock() >= deadline_at:
            raise ProviderDispatchError(
                ProviderDispatchErrorCode.DEADLINE
            )


def _reservation_id(
    request: ProviderDispatchRequest,
    attempt: int,
) -> str:
    evidence = (
        f"{request.workspace_id}:{request.turn_id}:"
        f"{request.provider_request_sha256}:{attempt}"
    ).encode()
    digest = hashlib.sha256(evidence).hexdigest()
    return f"pcs_{digest[:32]}"
