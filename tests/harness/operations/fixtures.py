"""Canonical durable operation test fixtures."""

from datetime import UTC, datetime, timedelta

from app.services.harness.protocol import (
    IdempotencyClass,
    OperationLimits,
)
from app.services.harness.protocol.operation_admission import (
    CanonicalOperationRequest,
    OperationFence,
    canonical_operation_args_sha256,
    stable_operation_id,
)

NOW = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
WORKSPACE_ID = "wsp_" + "1" * 32
TURN_ID = "trn_" + "2" * 32
ARGS_SHA256 = canonical_operation_args_sha256(
    {"path": "src/app.py", "content_sha256": "3" * 64}
)
OPERATION_ID = stable_operation_id(
    workspace_id=WORKSPACE_ID,
    turn_id=TURN_ID,
    idempotency_key="operation-command-0001",
    tool_name="workspace.write_file",
    tool_version="1.0.0",
    args_sha256=ARGS_SHA256,
    attempt=1,
)


def request() -> CanonicalOperationRequest:
    return CanonicalOperationRequest(
        workspace_id=WORKSPACE_ID,
        turn_id=TURN_ID,
        idempotency_key="operation-command-0001",
        idempotency_class=IdempotencyClass.NON_IDEMPOTENT,
        attempt=1,
        tool_name="workspace.write_file",
        tool_version="1.0.0",
        args_sha256=ARGS_SHA256,
        capability="filesystem.write",
        policy_decision_id="dcs_" + "4" * 32,
        approval_id="apr_" + "5" * 32,
        limits=OperationLimits(
            max_duration_ms=600_000,
            max_cpu_ms=600_000,
            max_memory_bytes=512 * 1024 * 1024,
            max_output_bytes=1024 * 1024,
            max_processes=16,
        ),
        operation_id=OPERATION_ID,
    )


def fence(*, fencing_token: int = 7) -> OperationFence:
    return OperationFence(
        workspace_id=WORKSPACE_ID,
        operation_id=OPERATION_ID,
        lease_sha256="6" * 64,
        fencing_token=fencing_token,
        issued_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
    )


class RecordingStore:
    def __init__(self, *, fail_on_write: int | None = None) -> None:
        self.records = []
        self.receipts = []
        self.fail_on_write = fail_on_write

    async def save_operation(
        self,
        workspace_id,
        operation,
        *,
        updated_at,
    ):
        if self.fail_on_write == len(self.records) + 1:
            raise RuntimeError("injected durability failure")
        self.records.append((workspace_id, operation, updated_at))
        return operation

    async def save_admission(
        self,
        workspace_id,
        operation,
        receipt,
        *,
        updated_at,
    ):
        if self.fail_on_write == len(self.records) + 1:
            raise RuntimeError("injected durability failure")
        self.records.append((workspace_id, operation, updated_at))
        self.receipts.append(receipt)
        return operation, receipt
