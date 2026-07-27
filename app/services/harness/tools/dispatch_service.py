"""Durability-first dispatch for the fixed built-in tool catalog."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import BaseModel

from app.services.harness.protocol import (
    IdempotencyClass,
    WorkspaceId,
)
from app.services.harness.protocol.operation_admission import (
    OperationDispatchPermit,
)
from app.services.harness.tools.builtin_executor import (
    BuiltinToolExecutor,
    ProcessDispatchContext,
)
from app.services.harness.tools.contracts import ValidatedToolCall
from app.services.harness.tools.dispatch_admission import OperationClaimGuard
from app.services.harness.tools.dispatch_contracts import (
    ToolDispatchOutcome,
    ToolDispatchStatus,
)
from app.services.harness.tools.dispatch_support import (
    build_dispatch_outcome,
    canonical_dispatch_json,
    classify_tool_error,
    validate_dispatch_permit,
)
from app.services.harness.tools.file_errors import WorkspaceFileError
from app.services.harness.tools.file_service import WorkspaceFileService
from app.services.harness.tools.operation_lifecycle import (
    DurableOperationLifecycle,
)
from app.services.harness.tools.process_service import (
    ProcessToolError,
    SandboxedProcessService,
)
from app.services.harness.tools.registry import (
    ToolRegistry,
)


class ToolDispatchDurabilityError(RuntimeError):
    """A terminal outcome could not be made durable."""


class BuiltinToolDispatcher:
    def __init__(
        self,
        registry: ToolRegistry,
        files: WorkspaceFileService,
        processes: SandboxedProcessService,
        lifecycle: DurableOperationLifecycle,
        *,
        workspace_id: WorkspaceId,
        clock: Callable[[], datetime],
        maximum_claimed_operations: int = 4096,
    ) -> None:
        self._registry = registry
        self._executor = BuiltinToolExecutor(files, processes)
        self._lifecycle = lifecycle
        self._workspace_id = workspace_id
        self._clock = clock
        self._claims = OperationClaimGuard(maximum_claimed_operations)

    async def dispatch(
        self,
        call: ValidatedToolCall,
        permit: OperationDispatchPermit,
        *,
        cancellation: asyncio.Event,
        process: ProcessDispatchContext | None = None,
    ) -> ToolDispatchOutcome:
        try:
            paired_call = ValidatedToolCall.model_validate(call.model_dump())
            verified_permit = OperationDispatchPermit.model_validate(
                permit.model_dump()
            )
        except Exception:
            raise ToolDispatchDurabilityError(
                "tool call and permit are not pairable evidence"
            ) from None
        if (
            verified_permit.prepared_receipt.workspace_id
            != self._workspace_id
            or verified_permit.dispatched_receipt.workspace_id
            != self._workspace_id
        ):
            raise ToolDispatchDurabilityError(
                "dispatch permit belongs to another workspace"
            )
        await self._claims.claim(verified_permit.operation.operation_id)
        try:
            verified_call = self._registry.revalidate_call(paired_call)
            verified_permit = validate_dispatch_permit(
                verified_call,
                verified_permit,
                self._workspace_id,
            )
        except Exception:
            return await self._terminal_error(
                paired_call,
                verified_permit,
                status=ToolDispatchStatus.FAILED,
                code="dispatch_evidence_rejected",
                message="Tool dispatch evidence was rejected.",
            )

        try:
            result = await self._executor.execute(
                verified_call,
                verified_permit,
                cancellation=cancellation,
                process=process,
            )
        except asyncio.CancelledError:
            return await self._terminal_error(
                verified_call,
                verified_permit,
                status=ToolDispatchStatus.CANCELLED,
                code="cancelled",
                message="Tool execution was cancelled.",
            )
        except (WorkspaceFileError, ProcessToolError) as error:
            status, code, message = classify_tool_error(error)
            return await self._terminal_error(
                verified_call,
                verified_permit,
                status=status,
                code=code,
                message=message,
            )
        except Exception:
            status = (
                ToolDispatchStatus.AMBIGUOUS
                if verified_call.idempotency_class
                is IdempotencyClass.NON_IDEMPOTENT
                else ToolDispatchStatus.FAILED
            )
            return await self._terminal_error(
                verified_call,
                verified_permit,
                status=status,
                code="execution_failed",
                message="Tool execution failed.",
            )
        return await self._complete(
            verified_call,
            verified_permit,
            result,
        )

    async def _complete(
        self,
        call: ValidatedToolCall,
        permit: OperationDispatchPermit,
        result: BaseModel,
    ) -> ToolDispatchOutcome:
        try:
            result_json = canonical_dispatch_json(
                result.model_dump(mode="json")
            )
        except Exception:
            status = (
                ToolDispatchStatus.AMBIGUOUS
                if call.idempotency_class is IdempotencyClass.NON_IDEMPOTENT
                else ToolDispatchStatus.FAILED
            )
            return await self._terminal_error(
                call,
                permit,
                status=status,
                code="result_filter",
                message="Tool result normalization failed.",
            )
        result_bytes = result_json.encode()
        result_sha256 = hashlib.sha256(result_bytes).hexdigest()
        response_json = result_json
        if len(result_bytes) > call.output.maximum_inline_bytes:
            if call.idempotency_class is IdempotencyClass.READ_ONLY:
                return await self._terminal_error(
                    call,
                    permit,
                    status=ToolDispatchStatus.FAILED,
                    code="result_limit",
                    message="Tool result exceeded its inline limit.",
                )
            response_json = canonical_dispatch_json(
                {
                    "error": {
                        "code": "result_limit_after_completion",
                        "message": (
                            "Tool completed but its result exceeded the "
                            "inline limit."
                        ),
                    },
                    "result_bytes": len(result_bytes),
                    "result_sha256": result_sha256,
                }
            )
        completed_at = self._now()
        try:
            operation = await self._lifecycle.complete(
                permit,
                workspace_id=self._workspace_id,
                completed_at=completed_at,
                result_sha256=result_sha256,
            )
        except Exception as error:
            raise ToolDispatchDurabilityError(
                "completed tool outcome was not durable"
            ) from error
        return build_dispatch_outcome(
            call,
            permit,
            operation,
            status=ToolDispatchStatus.COMPLETED,
            response_json=response_json,
            execution_result_sha256=result_sha256,
            execution_result_bytes=len(result_bytes),
        )

    async def _terminal_error(
        self,
        call: ValidatedToolCall,
        permit: OperationDispatchPermit,
        *,
        status: ToolDispatchStatus,
        code: str,
        message: str,
    ) -> ToolDispatchOutcome:
        observed_at = self._now()
        try:
            if status is ToolDispatchStatus.CANCELLED:
                operation = await self._lifecycle.cancel_dispatched(
                    permit,
                    workspace_id=self._workspace_id,
                    cancelled_at=observed_at,
                    reason=message,
                )
            elif status is ToolDispatchStatus.AMBIGUOUS:
                operation = await self._lifecycle.mark_ambiguous(
                    permit,
                    workspace_id=self._workspace_id,
                    observed_at=observed_at,
                    reason=message,
                )
            else:
                operation = await self._lifecycle.fail(
                    permit,
                    workspace_id=self._workspace_id,
                    failed_at=observed_at,
                    reason=message,
                )
        except Exception as error:
            raise ToolDispatchDurabilityError(
                "failed tool outcome was not durable"
            ) from error
        response_json = canonical_dispatch_json(
            {
                "error": {
                    "code": code,
                    "message": message,
                },
                "retry_allowed": False,
            }
        )
        return build_dispatch_outcome(
            call,
            permit,
            operation,
            status=status,
            response_json=response_json,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ToolDispatchDurabilityError(
                "tool dispatcher clock must return UTC"
            )
        return value
