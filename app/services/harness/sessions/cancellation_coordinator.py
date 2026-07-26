"""Bounded cooperative cancellation, force escalation, and evidence ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Never

from pydantic import TypeAdapter

from app.services.harness.protocol import TurnId
from app.services.harness.protocol.base import BoundedReason
from app.services.harness.sessions.cancellation_contracts import (
    MAXIMUM_OWNED_CANCELLATION_RESOURCES,
    CancellationEvidenceSink,
    CancellationReport,
    CancellationScopeState,
    CancellationSettlement,
    OwnedCancellationResource,
)
from app.services.harness.sessions.cancellation_operations import (
    build_report,
    invoke_resources,
    resource_results,
    settled_resources,
)

_REASON_ADAPTER = TypeAdapter(BoundedReason)


class CancellationCoordinatorErrorCode(StrEnum):
    CAPACITY = "capacity"
    DUPLICATE_RESOURCE = "duplicate_resource"
    EVIDENCE_FAILED = "evidence_failed"
    INVALID_STATE = "invalid_state"


class CancellationCoordinatorError(RuntimeError):
    def __init__(self, code: CancellationCoordinatorErrorCode) -> None:
        super().__init__("cancellation ownership operation rejected")
        self.code = code


class CancellationCoordinator:
    """Owns every registered resource until settlement or operator escalation."""

    def __init__(
        self,
        turn_id: TurnId,
        evidence_sink: CancellationEvidenceSink,
        *,
        maximum_resources: int = 128,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not 1 <= maximum_resources <= MAXIMUM_OWNED_CANCELLATION_RESOURCES:
            raise ValueError("cancellation resources must be between 1 and 256")
        self._turn_id = turn_id
        self._evidence_sink = evidence_sink
        self._maximum_resources = maximum_resources
        self._clock = clock
        self._resources: dict[str, OwnedCancellationResource] = {}
        self._state = CancellationScopeState.OPEN
        self._report: CancellationReport | None = None
        self._cancellation_event = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def cancellation_event(self) -> asyncio.Event:
        return self._cancellation_event

    async def register(self, resource: OwnedCancellationResource) -> None:
        identity = resource.cancellation_identity
        async with self._lock:
            self._require_open()
            existing = self._resources.get(identity.resource_id)
            if existing is resource:
                return
            if existing is not None:
                self._reject(
                    CancellationCoordinatorErrorCode.DUPLICATE_RESOURCE
                )
            if len(self._resources) >= self._maximum_resources:
                self._reject(CancellationCoordinatorErrorCode.CAPACITY)
            self._resources[identity.resource_id] = resource

    async def release(self, resource: OwnedCancellationResource) -> None:
        identity = resource.cancellation_identity
        async with self._lock:
            self._require_open()
            existing = self._resources.get(identity.resource_id)
            if existing is not resource:
                self._reject(
                    CancellationCoordinatorErrorCode.DUPLICATE_RESOURCE
                )
            del self._resources[identity.resource_id]

    async def active_resources(self) -> int:
        async with self._lock:
            return len(self._resources)

    async def state(self) -> CancellationScopeState:
        async with self._lock:
            return self._state

    async def last_report(self) -> CancellationReport | None:
        async with self._lock:
            return self._report

    async def cancel(
        self,
        reason: str,
        *,
        evidence_timeout_seconds: float = 1,
        cooperative_settle_timeout_seconds: float = 5,
        forced_settle_timeout_seconds: float = 5,
    ) -> CancellationReport:
        self._validate_timeout(evidence_timeout_seconds, "evidence")
        self._validate_timeout(
            cooperative_settle_timeout_seconds,
            "cooperative settlement",
        )
        self._validate_timeout(
            forced_settle_timeout_seconds,
            "forced settlement",
        )
        validated_reason = _REASON_ADAPTER.validate_python(reason, strict=True)
        requested_at = self._utc_now()
        cancellation_task = asyncio.create_task(
            self._cancel_owned(
                validated_reason,
                requested_at,
                evidence_timeout_seconds=evidence_timeout_seconds,
                cooperative_settle_timeout_seconds=(
                    cooperative_settle_timeout_seconds
                ),
                forced_settle_timeout_seconds=forced_settle_timeout_seconds,
            )
        )
        try:
            return await asyncio.shield(cancellation_task)
        except asyncio.CancelledError:
            await asyncio.gather(cancellation_task, return_exceptions=True)
            raise

    async def _cancel_owned(
        self,
        reason: str,
        requested_at: datetime,
        *,
        evidence_timeout_seconds: float,
        cooperative_settle_timeout_seconds: float,
        forced_settle_timeout_seconds: float,
    ) -> CancellationReport:
        existing_report, resources = await self._begin_cancellation()
        if existing_report is not None:
            return existing_report
        maximum_duration = (
            evidence_timeout_seconds
            + cooperative_settle_timeout_seconds
            + forced_settle_timeout_seconds
        )
        deadline_at = requested_at + timedelta(seconds=maximum_duration)
        cancel_attempts = invoke_resources(
            resources,
            lambda resource: resource.request_cancel(),
        )
        cooperative = await settled_resources(
            resources,
            cooperative_settle_timeout_seconds,
        )
        escalation_resources = {
            resource_id: resource
            for resource_id, resource in resources.items()
            if resource_id not in cooperative
        }
        force_attempts = invoke_resources(
            escalation_resources,
            lambda resource: resource.force_terminate(),
        )
        after_escalation = await settled_resources(
            escalation_resources,
            forced_settle_timeout_seconds,
        )
        results = resource_results(
            resources,
            cancel_attempts,
            cooperative,
            force_attempts,
            after_escalation,
        )
        completed_at = self._utc_now()
        report = build_report(
            self._turn_id,
            reason,
            requested_at,
            deadline_at,
            completed_at,
            results,
        )
        await self._finish_cancellation(
            report,
            resources,
            evidence_timeout_seconds,
        )
        return report

    async def _begin_cancellation(
        self,
    ) -> tuple[
        CancellationReport | None,
        dict[str, OwnedCancellationResource],
    ]:
        async with self._lock:
            if self._report is not None:
                return self._report, {}
            if self._state is not CancellationScopeState.OPEN:
                self._reject(CancellationCoordinatorErrorCode.INVALID_STATE)
            self._state = CancellationScopeState.CANCELLING
            self._cancellation_event.set()
            return None, dict(self._resources)

    async def _finish_cancellation(
        self,
        report: CancellationReport,
        resources: dict[str, OwnedCancellationResource],
        evidence_timeout_seconds: float,
    ) -> None:
        evidence_recorded = await self._record_evidence(
            report,
            evidence_timeout_seconds,
        )
        async with self._lock:
            failed_ids = {
                result.identity.resource_id
                for result in report.resources
                if result.settlement
                is CancellationSettlement.CONTAINMENT_FAILED
            }
            for resource_id, resource in resources.items():
                if (
                    resource_id not in failed_ids
                    and self._resources.get(resource_id) is resource
                ):
                    del self._resources[resource_id]
            self._report = report
            self._state = (
                report.state
                if evidence_recorded
                else CancellationScopeState.NEEDS_OPERATOR
            )
        if not evidence_recorded:
            self._reject(CancellationCoordinatorErrorCode.EVIDENCE_FAILED)

    async def _record_evidence(
        self,
        report: CancellationReport,
        timeout_seconds: float,
    ) -> bool:
        task = asyncio.create_task(
            self._evidence_sink.record_cancellation(report)
        )
        try:
            done, _pending = await asyncio.wait(
                {task},
                timeout=timeout_seconds,
            )
            return (
                task in done
                and not task.cancelled()
                and task.exception() is None
            )
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _require_open(self) -> None:
        if self._state is not CancellationScopeState.OPEN:
            self._reject(CancellationCoordinatorErrorCode.INVALID_STATE)

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("cancellation clock must return UTC")
        return value

    @staticmethod
    def _validate_timeout(value: float, label: str) -> None:
        if not 0.001 <= value <= 60:
            raise ValueError(f"{label} timeout must be between 0.001 and 60")

    @staticmethod
    def _reject(code: CancellationCoordinatorErrorCode) -> Never:
        raise CancellationCoordinatorError(code)
