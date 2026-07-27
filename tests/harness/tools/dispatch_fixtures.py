"""Canonical catalog calls and durable permits for dispatcher tests."""

import hashlib
import json
from datetime import datetime, timedelta

from app.services.harness.protocol import OperationLimits, ProviderToolCall
from app.services.harness.protocol.operation_admission import (
    CanonicalOperationRequest,
    OperationFence,
    stable_operation_id,
)
from app.services.harness.tools import (
    ValidatedToolCall,
    builtin_tool_registry,
)
from app.services.harness.tools.operation_lifecycle import (
    DurableOperationLifecycle,
)
from tests.harness.operations.fixtures import NOW, RecordingStore

WORKSPACE_ID = "wsp_" + "a" * 32
TURN_ID = "trn_" + "f" * 32


def terminal_time() -> datetime:
    return NOW + timedelta(seconds=2)


def catalog_call(
    name: str,
    arguments: dict[str, object],
    *,
    call_id: str,
) -> ValidatedToolCall:
    arguments_json = json.dumps(
        arguments,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    provider_call = ProviderToolCall(
        sequence=1,
        call_id=call_id,
        tool_name=name,
        arguments_json=arguments_json,
        arguments_sha256=hashlib.sha256(
            arguments_json.encode()
        ).hexdigest(),
    )
    return builtin_tool_registry().validate_provider_call(provider_call)


async def durable_dispatch(
    call: ValidatedToolCall,
    *,
    suffix: str,
    store: RecordingStore | None = None,
):
    durability_store = store or RecordingStore()
    idempotency_key = f"tool-dispatch-{suffix}-0001"
    operation_id = stable_operation_id(
        workspace_id=WORKSPACE_ID,
        turn_id=TURN_ID,
        idempotency_key=idempotency_key,
        tool_name=call.tool_name,
        tool_version=call.tool_version,
        args_sha256=call.args_sha256,
        attempt=1,
    )
    request = CanonicalOperationRequest(
        workspace_id=WORKSPACE_ID,
        turn_id=TURN_ID,
        idempotency_key=idempotency_key,
        idempotency_class=call.idempotency_class,
        attempt=1,
        tool_name=call.tool_name,
        tool_version=call.tool_version,
        args_sha256=call.args_sha256,
        capability=call.capability,
        policy_decision_id="dcs_" + "1" * 32,
        approval_id=(
            "apr_" + "2" * 32
            if call.idempotency_class.value == "non_idempotent"
            else None
        ),
        limits=OperationLimits(
            max_duration_ms=1000,
            max_cpu_ms=1000,
            max_memory_bytes=64 * 1024 * 1024,
            max_output_bytes=call.output.maximum_bytes,
            max_processes=4,
        ),
        operation_id=operation_id,
    )
    fence = OperationFence(
        workspace_id=WORKSPACE_ID,
        operation_id=operation_id,
        lease_sha256="3" * 64,
        fencing_token=5,
        issued_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=1),
    )
    lifecycle = DurableOperationLifecycle(durability_store)
    prepared = await lifecycle.prepare(request, fence, prepared_at=NOW)
    permit = await lifecycle.dispatch(
        prepared,
        fence,
        dispatched_at=NOW + timedelta(seconds=1),
    )
    return permit, lifecycle, durability_store
