"""Client disconnect, replay, checkpoint, and event-resync tests."""

import hashlib
import json
from datetime import UTC, datetime

import pytest

from app.cli.harness.client_contracts import build_command_envelope
from app.cli.harness.recovery import (
    ClientConnectionState,
    ClientRecoveryCoordinator,
    ReplayDecision,
)
from app.services.harness.protocol import (
    CommandEnvelope,
    CommandReplayReceipt,
    InlinePayload,
    command_result_sha256,
)
from app.services.harness.runtime import ResyncRequired

WORKSPACE = "wsp_" + "1" * 32
CLIENT = "cli_" + "2" * 32
REQUEST = "req_" + "3" * 32
PRINCIPAL = "prn_" + "4" * 32
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _envelope() -> CommandEnvelope:
    return build_command_envelope(
        command_payload={
            "kind": "thread.create",
            "retention_class": "standard",
            "idempotency_key": "idem-client-recovery-1",
        },
        workspace_id=WORKSPACE,
        client_id=CLIENT,
        request_id=REQUEST,
    )


def test_pending_command_survives_checkpoint_and_replays_once() -> None:
    coordinator = ClientRecoveryCoordinator(WORKSPACE)
    envelope = _envelope()
    request_hash = coordinator.register(envelope)
    assert request_hash is not None
    checkpoint = coordinator.checkpoint()
    restored = ClientRecoveryCoordinator(WORKSPACE)
    restored.restore(checkpoint)
    assert restored.plan_replay(envelope).decision is ReplayDecision.REPLAY

    result = InlinePayload(
        text="ok",
        size_bytes=2,
        content_sha256=hashlib.sha256(b"ok").hexdigest(),
    )
    receipt_data = {
        "workspace_id": WORKSPACE,
        "principal_id": PRINCIPAL,
        "idempotency_key": "idem-client-recovery-1",
        "command_kind": "thread.create",
        "request_sha256": request_hash,
        "response_kind": "thread_status",
        "result": result.model_dump(mode="json"),
        "result_sha256": command_result_sha256(result),
        "committed_at": NOW.isoformat(),
    }
    receipt = CommandReplayReceipt.model_validate_json(json.dumps(receipt_data))
    restored.record_receipt(receipt)
    assert restored.plan_replay(envelope).decision is ReplayDecision.ALREADY_COMMITTED


def test_disconnect_resync_and_acknowledgement_are_monotonic() -> None:
    coordinator = ClientRecoveryCoordinator(WORKSPACE)
    coordinator.disconnect()
    assert coordinator.snapshot().state is ClientConnectionState.DISCONNECTED
    coordinator.observe_delivery(
        ResyncRequired(
            subscription_id="sub_" + "5" * 32,
            resume_after_sequence=0,
            latest_observed_sequence=3,
        )
    )
    assert coordinator.snapshot().state is ClientConnectionState.RESYNC_REQUIRED
    with pytest.raises(ValueError, match="undelivered"):
        coordinator.acknowledge(4)


def test_pending_capacity_is_bounded() -> None:
    coordinator = ClientRecoveryCoordinator(
        WORKSPACE,
        maximum_pending_commands=1,
    )
    coordinator.register(_envelope())
    second = build_command_envelope(
        command_payload={
            "kind": "thread.create",
            "retention_class": "standard",
            "idempotency_key": "idem-client-recovery-2",
        },
        workspace_id=WORKSPACE,
        client_id=CLIENT,
        request_id="req_" + "6" * 32,
    )
    with pytest.raises(ValueError, match="capacity"):
        coordinator.register(second)


def test_changed_semantic_hash_cannot_replay_same_request_id() -> None:
    coordinator = ClientRecoveryCoordinator(WORKSPACE)
    original = _envelope()
    coordinator.register(original)
    changed = build_command_envelope(
        command_payload={
            "kind": "thread.create",
            "retention_class": "standard",
            "idempotency_key": "idem-client-recovery-2",
        },
        workspace_id=WORKSPACE,
        client_id=CLIENT,
        request_id=REQUEST,
    )
    assert coordinator.plan_replay(changed).decision is ReplayDecision.HASH_CHANGED
