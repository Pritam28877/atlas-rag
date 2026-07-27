"""Exact status-proof reconciliation tests."""

import asyncio
from datetime import timedelta

import pytest

from app.services.harness.protocol import OperationState
from app.services.harness.protocol.recovery import (
    OperationReconciliationOutcome,
    OperationStatusProof,
    OperationStatusProofKind,
    operation_status_proof_sha256,
)
from app.services.harness.runtime.recovery_classification import (
    OperationReconciliationError,
    reconcile_ambiguous_operation,
)
from app.services.harness.tools.operation_lifecycle import DurableOperationLifecycle
from tests.harness.operations.fixtures import (
    NOW,
    WORKSPACE_ID,
    RecordingStore,
    fence,
    request,
)


async def ambiguous_operation():
    lifecycle = DurableOperationLifecycle(RecordingStore())
    prepared = await lifecycle.prepare(request(), fence(), prepared_at=NOW)
    permit = await lifecycle.dispatch(
        prepared,
        fence(),
        dispatched_at=NOW + timedelta(seconds=1),
    )
    return await lifecycle.mark_ambiguous(
        permit,
        workspace_id=WORKSPACE_ID,
        observed_at=NOW + timedelta(seconds=2),
        reason="Outcome unavailable after disconnect.",
    )


def proof(operation, kind: OperationStatusProofKind) -> OperationStatusProof:
    result_sha256 = (
        "c" * 64 if kind is OperationStatusProofKind.COMPLETED else None
    )
    failure_reason = (
        "Tool proved failure."
        if kind is OperationStatusProofKind.FAILED
        else None
    )
    observed_at = NOW + timedelta(seconds=3)
    proof_sha256 = operation_status_proof_sha256(
        operation_id=operation.operation_id,
        tool_name=operation.tool_name,
        tool_version=operation.tool_version,
        args_sha256=operation.args_sha256,
        fencing_token=operation.lease_fencing_token,
        kind=kind,
        status_request_sha256="a" * 64,
        status_response_sha256="b" * 64,
        result_sha256=result_sha256,
        failure_reason=failure_reason,
        observed_at=observed_at,
    )
    return OperationStatusProof(
        operation_id=operation.operation_id,
        tool_name=operation.tool_name,
        tool_version=operation.tool_version,
        args_sha256=operation.args_sha256,
        fencing_token=operation.lease_fencing_token,
        kind=kind,
        status_request_sha256="a" * 64,
        status_response_sha256="b" * 64,
        result_sha256=result_sha256,
        failure_reason=failure_reason,
        observed_at=observed_at,
        proof_sha256=proof_sha256,
    )


@pytest.mark.parametrize(
    ("kind", "expected_state"),
    (
        (OperationStatusProofKind.COMPLETED, OperationState.COMPLETED),
        (OperationStatusProofKind.FAILED, OperationState.FAILED),
    ),
)
def test_exact_proof_can_close_ambiguous_operation(kind, expected_state) -> None:
    operation = asyncio.run(ambiguous_operation())
    decision = reconcile_ambiguous_operation(operation, proof(operation, kind))

    assert decision.outcome is OperationReconciliationOutcome.TERMINAL_PROVEN
    assert decision.operation.state is expected_state


def test_not_started_proof_allows_retry_without_rewriting_history() -> None:
    operation = asyncio.run(ambiguous_operation())
    decision = reconcile_ambiguous_operation(
        operation,
        proof(operation, OperationStatusProofKind.NOT_STARTED),
    )

    assert decision.outcome is (
        OperationReconciliationOutcome.RETRY_PROVEN_NOT_STARTED
    )
    assert decision.operation == operation


def test_mutated_args_fence_or_tool_cannot_reconcile() -> None:
    operation = asyncio.run(ambiguous_operation())
    exact = proof(operation, OperationStatusProofKind.COMPLETED)
    for field_name, value in (
        ("args_sha256", "d" * 64),
        ("fencing_token", operation.lease_fencing_token + 1),
        ("tool_version", "2.0.0"),
    ):
        mutated = exact.model_copy(update={field_name: value})
        with pytest.raises(OperationReconciliationError, match="does not match"):
            reconcile_ambiguous_operation(operation, mutated)
