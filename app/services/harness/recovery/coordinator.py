"""Integrity-first, bounded, and repeatable startup recovery orchestration."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from pydantic import Field

from app.services.harness.journal.health import JournalVerificationResult
from app.services.harness.journal.projection_contracts import ProjectionDefinition
from app.services.harness.journal.projection_online import (
    prepare_online_projections,
)
from app.services.harness.protocol import (
    OperationRecord,
    StrictProtocolModel,
    UtcTimestamp,
    WorkspaceId,
)
from app.services.harness.protocol.recovery import (
    OperationRecoveryAction,
    RecoveryLease,
)
from app.services.harness.runtime import (
    classify_operation_recovery,
    expire_recovery_lease,
)

MAXIMUM_RECOVERY_RECORDS = 10_000
MAXIMUM_RECOVERY_PROJECTIONS = 32


class StartupRecoveryStatus(StrEnum):
    READY = "ready"
    NEEDS_OPERATOR = "needs_operator"


class RecoveryCoordinatorErrorCode(StrEnum):
    INTEGRITY_LIMIT = "integrity_limit"


class RecoveryCoordinatorError(RuntimeError):
    def __init__(self, code: RecoveryCoordinatorErrorCode) -> None:
        super().__init__("startup recovery failed")
        self.code = code


class StartupRecoveryReport(StrictProtocolModel):
    workspace_id: WorkspaceId
    status: StartupRecoveryStatus
    verified_records: int = Field(ge=0, le=1_000_000)
    projections_rebuilt: int = Field(
        ge=0,
        le=MAXIMUM_RECOVERY_PROJECTIONS,
    )
    operations_checked: int = Field(ge=0, le=MAXIMUM_RECOVERY_RECORDS)
    prepared_operations: int = Field(ge=0, le=MAXIMUM_RECOVERY_RECORDS)
    retryable_operations: int = Field(ge=0, le=MAXIMUM_RECOVERY_RECORDS)
    needs_operator_operations: int = Field(ge=0, le=MAXIMUM_RECOVERY_RECORDS)
    active_leases: int = Field(ge=0, le=MAXIMUM_RECOVERY_RECORDS)
    expired_leases: int = Field(ge=0, le=MAXIMUM_RECOVERY_RECORDS)
    recovered_at: UtcTimestamp


class RecoveryIntegrityVerifier(Protocol):
    async def verify(
        self,
        *,
        maximum_records: int,
    ) -> JournalVerificationResult: ...


class RecoveryProjectionRebuilder(Protocol):
    async def rebuild(
        self,
        definition: ProjectionDefinition[Any],
        workspace_id: WorkspaceId,
        *,
        maximum_pages: int,
    ) -> object: ...


class RecoveryEvidenceStore(Protocol):
    async def load_recoverable_operations(
        self,
        workspace_id: WorkspaceId,
        *,
        maximum_records: int,
    ) -> tuple[OperationRecord, ...]: ...

    async def save_operation(
        self,
        workspace_id: WorkspaceId,
        operation: OperationRecord,
        *,
        updated_at: datetime,
    ) -> OperationRecord: ...

    async def load_active_leases(
        self,
        workspace_id: WorkspaceId,
        *,
        maximum_records: int,
    ) -> tuple[RecoveryLease, ...]: ...

    async def save_lease(
        self,
        workspace_id: WorkspaceId,
        lease: RecoveryLease,
        *,
        updated_at: datetime,
    ) -> RecoveryLease: ...


class RecoveryCoordinator:
    def __init__(
        self,
        integrity_verifier: RecoveryIntegrityVerifier,
        projection_rebuilder: RecoveryProjectionRebuilder,
        evidence_store: RecoveryEvidenceStore,
        projections: Sequence[ProjectionDefinition[Any]],
    ) -> None:
        self._integrity_verifier = integrity_verifier
        self._projection_rebuilder = projection_rebuilder
        self._evidence_store = evidence_store
        self._projections = prepare_online_projections(projections)

    async def recover(
        self,
        workspace_id: WorkspaceId,
        *,
        recovered_at: datetime,
        maximum_verification_records: int = 100_000,
        maximum_projection_pages: int = 1_024,
        maximum_recovery_records: int = MAXIMUM_RECOVERY_RECORDS,
    ) -> StartupRecoveryReport:
        self._require_utc(recovered_at)
        verification = await self._integrity_verifier.verify(
            maximum_records=maximum_verification_records
        )
        if not verification.complete:
            raise RecoveryCoordinatorError(
                RecoveryCoordinatorErrorCode.INTEGRITY_LIMIT
            )

        for definition in self._projections:
            await self._projection_rebuilder.rebuild(
                definition,
                workspace_id,
                maximum_pages=maximum_projection_pages,
            )

        operations = await self._evidence_store.load_recoverable_operations(
            workspace_id,
            maximum_records=maximum_recovery_records,
        )
        action_counts = await self._recover_operations(
            workspace_id,
            operations,
            recovered_at,
        )
        leases = await self._evidence_store.load_active_leases(
            workspace_id,
            maximum_records=maximum_recovery_records,
        )
        active_leases, expired_leases = await self._recover_leases(
            workspace_id,
            leases,
            recovered_at,
        )
        needs_operator = action_counts[OperationRecoveryAction.NEEDS_OPERATOR]
        return StartupRecoveryReport(
            workspace_id=workspace_id,
            status=(
                StartupRecoveryStatus.NEEDS_OPERATOR
                if needs_operator
                else StartupRecoveryStatus.READY
            ),
            verified_records=(
                verification.events_checked
                + verification.receipts_checked
                + verification.projections_checked
            ),
            projections_rebuilt=len(self._projections),
            operations_checked=len(operations),
            prepared_operations=action_counts[
                OperationRecoveryAction.RESUME_PREPARED
            ],
            retryable_operations=action_counts[
                OperationRecoveryAction.RETRY_IDEMPOTENT
            ],
            needs_operator_operations=needs_operator,
            active_leases=active_leases,
            expired_leases=expired_leases,
            recovered_at=recovered_at,
        )

    async def _recover_operations(
        self,
        workspace_id: WorkspaceId,
        operations: tuple[OperationRecord, ...],
        recovered_at: datetime,
    ) -> dict[OperationRecoveryAction, int]:
        action_counts = {
            action: 0
            for action in OperationRecoveryAction
        }
        for operation in operations:
            decision = classify_operation_recovery(
                operation,
                recovered_at=recovered_at,
            )
            action_counts[decision.action] += 1
            if decision.operation != operation:
                await self._evidence_store.save_operation(
                    workspace_id,
                    decision.operation,
                    updated_at=recovered_at,
                )
        return action_counts

    async def _recover_leases(
        self,
        workspace_id: WorkspaceId,
        leases: tuple[RecoveryLease, ...],
        recovered_at: datetime,
    ) -> tuple[int, int]:
        active_count = 0
        expired_count = 0
        for lease in leases:
            recovered = expire_recovery_lease(
                lease,
                recovered_at=recovered_at,
            )
            if recovered == lease:
                active_count += 1
                continue
            await self._evidence_store.save_lease(
                workspace_id,
                recovered,
                updated_at=recovered_at,
            )
            expired_count += 1
        return active_count, expired_count

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("recovery timestamp must use UTC")
