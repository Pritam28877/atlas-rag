"""Pure validation, classification, and pairing helpers for tool dispatch."""

import hashlib
import json

from app.services.harness.protocol import (
    OperationRecord,
    WorkspaceId,
)
from app.services.harness.protocol.operation_admission import (
    OperationDispatchPermit,
)
from app.services.harness.tools.contracts import ValidatedToolCall
from app.services.harness.tools.dispatch_contracts import (
    ToolDispatchOutcome,
    ToolDispatchStatus,
)
from app.services.harness.tools.file_errors import (
    WorkspaceFileError,
    WorkspaceFileErrorCode,
)
from app.services.harness.tools.process_service import (
    ProcessToolError,
    ProcessToolErrorCode,
)


def validate_dispatch_permit(
    call: ValidatedToolCall,
    permit: OperationDispatchPermit,
    workspace_id: WorkspaceId,
) -> OperationDispatchPermit:
    verified = OperationDispatchPermit.model_validate(permit.model_dump())
    operation = verified.operation
    prepared = verified.prepared_receipt
    dispatched = verified.dispatched_receipt
    if (
        operation.tool_name != call.tool_name
        or operation.tool_version != call.tool_version
        or operation.args_sha256 != call.args_sha256
        or operation.capability != call.capability
        or operation.idempotency_class is not call.idempotency_class
        or prepared.operation_id != operation.operation_id
        or dispatched.operation_id != operation.operation_id
        or prepared.workspace_id != workspace_id
        or dispatched.workspace_id != workspace_id
        or prepared.fencing_token != operation.lease_fencing_token
        or dispatched.fencing_token != operation.lease_fencing_token
    ):
        raise ValueError("tool dispatch permit does not match the call")
    return verified


def classify_tool_error(
    error: WorkspaceFileError | ProcessToolError,
) -> tuple[ToolDispatchStatus, str, str]:
    if isinstance(error, WorkspaceFileError):
        if error.code is WorkspaceFileErrorCode.CANCELLED:
            return (
                ToolDispatchStatus.CANCELLED,
                "cancelled",
                "Tool execution was cancelled.",
            )
        if error.code is WorkspaceFileErrorCode.AMBIGUOUS:
            return (
                ToolDispatchStatus.AMBIGUOUS,
                "ambiguous",
                "Tool completion could not be confirmed.",
            )
        return (
            ToolDispatchStatus.FAILED,
            error.code.value,
            "Workspace file operation failed.",
        )
    if error.code is ProcessToolErrorCode.CANCELLED:
        return (
            ToolDispatchStatus.CANCELLED,
            "cancelled",
            "Tool execution was cancelled.",
        )
    if error.code is ProcessToolErrorCode.PERMIT:
        return (
            ToolDispatchStatus.FAILED,
            "process_permit",
            "Process execution evidence was rejected.",
        )
    return (
        ToolDispatchStatus.AMBIGUOUS,
        error.code.value,
        "Process completion could not be confirmed.",
    )


def build_dispatch_outcome(
    call: ValidatedToolCall,
    permit: OperationDispatchPermit,
    operation: OperationRecord,
    *,
    status: ToolDispatchStatus,
    response_json: str,
    execution_result_sha256: str | None = None,
    execution_result_bytes: int | None = None,
) -> ToolDispatchOutcome:
    response = response_json.encode()
    return ToolDispatchOutcome(
        call_id=call.call_id,
        operation_id=operation.operation_id,
        tool_name=operation.tool_name,
        tool_version=operation.tool_version,
        call_descriptor_sha256=call.descriptor_sha256,
        prepared_receipt_sha256=permit.prepared_receipt.receipt_sha256,
        dispatched_receipt_sha256=permit.dispatched_receipt.receipt_sha256,
        status=status,
        response_json=response_json,
        response_sha256=hashlib.sha256(response).hexdigest(),
        response_bytes=len(response),
        execution_result_sha256=execution_result_sha256,
        execution_result_bytes=execution_result_bytes,
        operation=operation,
    )


def canonical_dispatch_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
