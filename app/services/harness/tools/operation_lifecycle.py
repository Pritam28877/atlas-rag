"""Durability-first admission and typed terminal operation transitions."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.services.harness.protocol import (
    ArtifactId,
    OperationRecord,
    OperationState,
    Sha256,
    WorkspaceId,
)
from app.services.harness.protocol.operation_admission import (
    CanonicalOperationRequest,
    OperationDispatchPermit,
    OperationDurability,
    OperationDurabilityReceipt,
    OperationFence,
    PreparedOperation,
    operation_fence_sha256,
    operation_receipt_sha256,
    operation_record_sha256,
)


class OperationDurabilityError(RuntimeError):
    """Stable rejection before an executor may observe a dispatch permit."""


class OperationDurabilityStore(Protocol):
    async def save_operation(
        self,
        workspace_id: WorkspaceId,
        operation: OperationRecord,
        *,
        updated_at: datetime,
    ) -> OperationRecord: ...

    async def save_admission(
        self,
        workspace_id: WorkspaceId,
        operation: OperationRecord,
        receipt: OperationDurabilityReceipt,
        *,
        updated_at: datetime,
    ) -> tuple[OperationRecord, OperationDurabilityReceipt]: ...


class DurableOperationLifecycle:
    def __init__(self, store: OperationDurabilityStore) -> None:
        self._store = store

    async def prepare(
        self,
        request: CanonicalOperationRequest,
        fence: OperationFence,
        *,
        prepared_at: datetime,
    ) -> PreparedOperation:
        _validate_fence(request, fence, prepared_at)
        operation = OperationRecord(
            operation_id=request.operation_id,
            turn_id=request.turn_id,
            idempotency_key=request.idempotency_key,
            idempotency_class=request.idempotency_class,
            attempt=request.attempt,
            lease_fencing_token=fence.fencing_token,
            tool_name=request.tool_name,
            tool_version=request.tool_version,
            args_sha256=request.args_sha256,
            capability=request.capability,
            policy_decision_id=request.policy_decision_id,
            approval_id=request.approval_id,
            state=OperationState.PREPARED,
            limits=request.limits,
            prepared_at=prepared_at,
        )
        receipt = _durability_receipt(
            workspace_id=request.workspace_id,
            operation=operation,
            durable_at=prepared_at,
            previous_receipt_sha256=None,
        )
        durable, durable_receipt = await self._store.save_admission(
            request.workspace_id,
            operation,
            receipt,
            updated_at=prepared_at,
        )
        _require_exact_durable_record(operation, durable)
        _require_exact_durable_receipt(receipt, durable_receipt)
        return PreparedOperation(
            operation=operation,
            fence_sha256=operation_fence_sha256(fence),
            receipt=receipt,
        )

    async def dispatch(
        self,
        prepared: PreparedOperation,
        fence: OperationFence,
        *,
        dispatched_at: datetime,
    ) -> OperationDispatchPermit:
        _validate_prepared_fence(prepared, fence, dispatched_at)
        operation = OperationRecord.model_validate(
            {
                **prepared.operation.model_dump(),
                "state": OperationState.DISPATCHED,
                "dispatched_at": dispatched_at,
            }
        )
        receipt = _durability_receipt(
            workspace_id=fence.workspace_id,
            operation=operation,
            durable_at=dispatched_at,
            previous_receipt_sha256=prepared.receipt.receipt_sha256,
        )
        durable, durable_receipt = await self._store.save_admission(
            fence.workspace_id,
            operation,
            receipt,
            updated_at=dispatched_at,
        )
        _require_exact_durable_record(operation, durable)
        _require_exact_durable_receipt(receipt, durable_receipt)
        return OperationDispatchPermit(
            operation=operation,
            fence_sha256=prepared.fence_sha256,
            prepared_receipt=prepared.receipt,
            dispatched_receipt=receipt,
        )

    async def complete(
        self,
        permit: OperationDispatchPermit,
        *,
        workspace_id: WorkspaceId,
        completed_at: datetime,
        result_sha256: Sha256,
        result_artifact_id: ArtifactId | None = None,
    ) -> OperationRecord:
        completed = OperationRecord.model_validate(
            {
                **permit.operation.model_dump(),
                "state": OperationState.COMPLETED,
                "terminal_at": completed_at,
                "result_sha256": result_sha256,
                "result_artifact_id": result_artifact_id,
            }
        )
        return await self._save_terminal(workspace_id, completed, completed_at)

    async def fail(
        self,
        permit: OperationDispatchPermit,
        *,
        workspace_id: WorkspaceId,
        failed_at: datetime,
        reason: str,
    ) -> OperationRecord:
        failed = OperationRecord.model_validate(
            {
                **permit.operation.model_dump(),
                "state": OperationState.FAILED,
                "terminal_at": failed_at,
                "status_reason": reason,
            }
        )
        return await self._save_terminal(workspace_id, failed, failed_at)

    async def mark_ambiguous(
        self,
        permit: OperationDispatchPermit,
        *,
        workspace_id: WorkspaceId,
        observed_at: datetime,
        reason: str,
    ) -> OperationRecord:
        ambiguous = OperationRecord.model_validate(
            {
                **permit.operation.model_dump(),
                "state": OperationState.AMBIGUOUS,
                "ambiguous_at": observed_at,
                "status_reason": reason,
            }
        )
        return await self._save_terminal(workspace_id, ambiguous, observed_at)

    async def cancel_prepared(
        self,
        prepared: PreparedOperation,
        *,
        workspace_id: WorkspaceId,
        cancelled_at: datetime,
        reason: str,
    ) -> OperationRecord:
        cancelled = OperationRecord.model_validate(
            {
                **prepared.operation.model_dump(),
                "state": OperationState.CANCELLED,
                "terminal_at": cancelled_at,
                "status_reason": reason,
            }
        )
        return await self._save_terminal(workspace_id, cancelled, cancelled_at)

    async def cancel_dispatched(
        self,
        permit: OperationDispatchPermit,
        *,
        workspace_id: WorkspaceId,
        cancelled_at: datetime,
        reason: str,
    ) -> OperationRecord:
        cancelled = OperationRecord.model_validate(
            {
                **permit.operation.model_dump(),
                "state": OperationState.CANCELLED,
                "terminal_at": cancelled_at,
                "status_reason": reason,
            }
        )
        return await self._save_terminal(workspace_id, cancelled, cancelled_at)

    async def _save_terminal(
        self,
        workspace_id: WorkspaceId,
        operation: OperationRecord,
        updated_at: datetime,
    ) -> OperationRecord:
        durable = await self._store.save_operation(
            workspace_id,
            operation,
            updated_at=updated_at,
        )
        _require_exact_durable_record(operation, durable)
        return durable


def _validate_fence(
    request: CanonicalOperationRequest,
    fence: OperationFence,
    observed_at: datetime,
) -> None:
    if (
        fence.workspace_id != request.workspace_id
        or fence.operation_id != request.operation_id
        or not fence.issued_at <= observed_at < fence.expires_at
    ):
        raise OperationDurabilityError("operation fence is not current")


def _validate_prepared_fence(
    prepared: PreparedOperation,
    fence: OperationFence,
    observed_at: datetime,
) -> None:
    if (
        fence.operation_id != prepared.operation.operation_id
        or fence.fencing_token != prepared.operation.lease_fencing_token
        or operation_fence_sha256(fence) != prepared.fence_sha256
        or not fence.issued_at <= observed_at < fence.expires_at
    ):
        raise OperationDurabilityError("operation fence is stale")


def _require_exact_durable_record(
    expected: OperationRecord,
    durable: OperationRecord,
) -> None:
    if durable != expected:
        raise OperationDurabilityError("operation durability evidence differs")


def _require_exact_durable_receipt(
    expected: OperationDurabilityReceipt,
    durable: OperationDurabilityReceipt,
) -> None:
    if durable != expected:
        raise OperationDurabilityError("operation receipt evidence differs")


def _durability_receipt(
    *,
    workspace_id: WorkspaceId,
    operation: OperationRecord,
    durable_at: datetime,
    previous_receipt_sha256: Sha256 | None,
) -> OperationDurabilityReceipt:
    operation_sha256 = operation_record_sha256(operation)
    receipt_sha256 = operation_receipt_sha256(
        workspace_id=workspace_id,
        operation_id=operation.operation_id,
        state=operation.state,
        fencing_token=operation.lease_fencing_token,
        operation_sha256=operation_sha256,
        previous_receipt_sha256=previous_receipt_sha256,
        durability=OperationDurability.SYNCHRONOUS,
        durable_at=durable_at,
    )
    return OperationDurabilityReceipt(
        workspace_id=workspace_id,
        operation_id=operation.operation_id,
        state=operation.state,
        fencing_token=operation.lease_fencing_token,
        operation_sha256=operation_sha256,
        previous_receipt_sha256=previous_receipt_sha256,
        durability=OperationDurability.SYNCHRONOUS,
        durable_at=durable_at,
        receipt_sha256=receipt_sha256,
    )
