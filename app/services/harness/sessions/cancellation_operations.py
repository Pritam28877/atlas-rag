"""Owned concurrent control calls used by the cancellation coordinator."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime

from app.services.harness.protocol import TurnId
from app.services.harness.sessions.cancellation_contracts import (
    CancellationControlOutcome,
    CancellationReport,
    CancellationResourceResult,
    CancellationScopeState,
    CancellationSettlement,
    OwnedCancellationResource,
)

ResourceOperation = Callable[[OwnedCancellationResource], None]


def build_report(
    turn_id: TurnId,
    reason: str,
    requested_at: datetime,
    deadline_at: datetime,
    completed_at: datetime,
    results: tuple[CancellationResourceResult, ...],
) -> CancellationReport:
    deadline_exceeded = completed_at > deadline_at
    containment_failed = any(
        result.settlement is CancellationSettlement.CONTAINMENT_FAILED
        for result in results
    )
    return CancellationReport(
        turn_id=turn_id,
        reason=reason,
        state=(
            CancellationScopeState.NEEDS_OPERATOR
            if containment_failed or deadline_exceeded
            else CancellationScopeState.CANCELLED
        ),
        requested_at=requested_at,
        deadline_at=deadline_at,
        completed_at=completed_at,
        deadline_exceeded=deadline_exceeded,
        resources=results,
    )


def invoke_resources(
    resources: dict[str, OwnedCancellationResource],
    operation: ResourceOperation,
) -> dict[str, CancellationControlOutcome]:
    outcomes: dict[str, CancellationControlOutcome] = {}
    for resource_id, resource in resources.items():
        try:
            operation(resource)
        except Exception:
            outcomes[resource_id] = CancellationControlOutcome.FAILED
        else:
            outcomes[resource_id] = CancellationControlOutcome.SUCCEEDED
    return outcomes


async def settled_resources(
    resources: dict[str, OwnedCancellationResource],
    timeout_seconds: float,
) -> set[str]:
    tasks: dict[str, asyncio.Task[None]] = {
        resource_id: asyncio.create_task(resource.wait_settled())
        for resource_id, resource in resources.items()
    }
    if not tasks:
        return set()
    try:
        _done, pending = await asyncio.wait(
            set(tasks.values()),
            timeout=timeout_seconds,
        )
    except BaseException:
        await cancel_tasks(tuple(tasks.values()))
        raise
    settled = {
        resource_id
        for resource_id, task in tasks.items()
        if task not in pending
        and not task.cancelled()
        and task.exception() is None
    }
    await cancel_tasks(tuple(pending))
    return settled


def resource_results(
    resources: dict[str, OwnedCancellationResource],
    cancel_attempts: dict[str, CancellationControlOutcome],
    cooperative: set[str],
    force_attempts: dict[str, CancellationControlOutcome],
    after_escalation: set[str],
) -> tuple[CancellationResourceResult, ...]:
    results: list[CancellationResourceResult] = []
    ordered = sorted(
        resources.values(),
        key=lambda resource: (
            resource.cancellation_identity.kind.value,
            resource.cancellation_identity.resource_id,
        ),
    )
    for resource in ordered:
        identity = resource.cancellation_identity
        if identity.resource_id in cooperative:
            settlement = CancellationSettlement.COOPERATIVE
            force = CancellationControlOutcome.NOT_ATTEMPTED
        elif identity.resource_id in after_escalation:
            settlement = CancellationSettlement.AFTER_ESCALATION
            force = force_attempts[identity.resource_id]
        else:
            settlement = CancellationSettlement.CONTAINMENT_FAILED
            force = force_attempts[identity.resource_id]
        results.append(
            CancellationResourceResult(
                identity=identity,
                cancel_request=cancel_attempts[identity.resource_id],
                force_request=force,
                settlement=settlement,
            )
        )
    return tuple(results)


async def cancel_tasks(tasks: tuple[asyncio.Task[None], ...]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
