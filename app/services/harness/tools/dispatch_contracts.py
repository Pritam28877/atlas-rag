"""Paired durable outcomes for one admitted built-in tool call."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    OperationId,
    OperationRecord,
    OperationState,
    Sha256,
    StrictProtocolModel,
)
from app.services.harness.protocol.execution import ToolName, ToolVersion
from app.services.harness.protocol.provider_stream import ProviderCallId
from app.services.harness.tools.contracts import DEFAULT_TOOL_OUTPUT_BYTES


class ToolDispatchStatus(StrEnum):
    AMBIGUOUS = "ambiguous"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class ToolDispatchOutcome(StrictProtocolModel):
    call_id: ProviderCallId
    operation_id: OperationId
    tool_name: ToolName
    tool_version: ToolVersion
    call_descriptor_sha256: Sha256
    prepared_receipt_sha256: Sha256
    dispatched_receipt_sha256: Sha256
    status: ToolDispatchStatus
    response_json: str = Field(
        min_length=2,
        max_length=DEFAULT_TOOL_OUTPUT_BYTES,
        repr=False,
    )
    response_sha256: Sha256
    response_bytes: int = Field(ge=2, le=DEFAULT_TOOL_OUTPUT_BYTES)
    execution_result_sha256: Sha256 | None = None
    execution_result_bytes: int | None = Field(
        default=None,
        ge=2,
        le=16 * 1024 * 1024,
    )
    retry_allowed: Literal[False] = False
    durable: Literal[True] = True
    operation: OperationRecord

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        try:
            response = json.loads(self.response_json)
            canonical = json.dumps(
                response,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (RecursionError, TypeError, ValueError) as error:
            raise ValueError("tool response is invalid") from error
        response_bytes = self.response_json.encode()
        expected_state = {
            ToolDispatchStatus.AMBIGUOUS: OperationState.AMBIGUOUS,
            ToolDispatchStatus.CANCELLED: OperationState.CANCELLED,
            ToolDispatchStatus.COMPLETED: OperationState.COMPLETED,
            ToolDispatchStatus.FAILED: OperationState.FAILED,
        }[self.status]
        completed = self.status is ToolDispatchStatus.COMPLETED
        if (
            not isinstance(response, dict)
            or canonical != self.response_json
            or len(response_bytes) != self.response_bytes
            or hashlib.sha256(response_bytes).hexdigest()
            != self.response_sha256
            or self.operation.operation_id != self.operation_id
            or self.operation.tool_name != self.tool_name
            or self.operation.tool_version != self.tool_version
            or self.operation.state is not expected_state
            or completed
            != (
                self.execution_result_sha256 is not None
                and self.execution_result_bytes is not None
            )
            or (
                completed
                and self.operation.result_sha256
                != self.execution_result_sha256
            )
        ):
            raise ValueError("tool outcome is not paired with its operation")
        return self
