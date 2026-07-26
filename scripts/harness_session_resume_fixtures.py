"""Deterministic authenticated turn fixtures for the session-resume probe."""

import hashlib
from datetime import UTC, datetime, timedelta

from app.services.harness.journal import (
    AppendRequest,
    JournalDurability,
)
from app.services.harness.protocol import (
    AuthenticationMethod,
    CommandEnvelope,
    CommandKind,
    DataClassification,
    EventActorKind,
    EventRecord,
    ExecutionBudget,
    GrantRecord,
    GrantState,
    InlinePayload,
    PrincipalRecord,
    TraceLink,
    TurnStartCommand,
    WorkspaceRecord,
    command_contract,
    command_request_sha256,
)
from app.services.harness.runtime import AuthenticatedCommandContext

NOW = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)
WORKSPACE_ID = "wsp_" + "1" * 32
PRINCIPAL_ID = "prn_" + "2" * 32
THREAD_ID = "thr_" + "3" * 32
TURN_ID = "trn_" + "4" * 32
EVENT_ID = "evt_" + "5" * 32
SUBSCRIPTION_ID = "sub_" + "6" * 32
POLICY_VERSION = "pol_" + "7" * 64
IDEMPOTENCY_KEY = "resume-drill-turn-0001"


def identifier(prefix: str, number: int) -> str:
    return f"{prefix}_{number:032x}"


def payload(text: str) -> InlinePayload:
    encoded = text.encode()
    return InlinePayload(
        text=text,
        size_bytes=len(encoded),
        content_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def command_context(connection_number: int) -> AuthenticatedCommandContext:
    principal = PrincipalRecord(
        principal_id=PRINCIPAL_ID,
        authentication_method=AuthenticationMethod.PEER_CREDENTIALS,
        issuer="atlas-resume-drill",
        subject_sha256="8" * 64,
        session_binding_sha256="9" * 64,
        authenticated_at=NOW - timedelta(minutes=1),
    )
    workspace = WorkspaceRecord(
        workspace_id=WORKSPACE_ID,
        tenant_id=identifier("ten", 1),
        owner_principal_id=PRINCIPAL_ID,
        root_uri="file:///srv/workspaces/resume-drill",
        repository_fingerprint_sha256="a" * 64,
        policy_version=POLICY_VERSION,
        created_at=NOW - timedelta(days=1),
    )
    grant = GrantRecord(
        grant_id=identifier("grt", 1),
        principal_id=PRINCIPAL_ID,
        workspace_id=WORKSPACE_ID,
        roles=("developer",),
        capabilities=("turn.start",),
        policy_version=POLICY_VERSION,
        state=GrantState.ACTIVE,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    envelope = CommandEnvelope(
        schema_version="1.2",
        request_id=identifier("req", connection_number),
        client_id=identifier("cli", connection_number),
        workspace_id=WORKSPACE_ID,
        expected_sequence=0,
        command=TurnStartCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            thread_id=THREAD_ID,
            requested_agent="coding-agent",
            budget=ExecutionBudget(
                max_steps=8,
                max_tool_calls=4,
                max_input_tokens=16_000,
                max_output_tokens=4_000,
                max_tool_output_bytes=4_096,
                max_duration_ms=30_000,
                max_cost_microusd=100_000,
            ),
            classification=DataClassification.INTERNAL,
            initial_payload=payload("resume drill request"),
        ),
    )
    return AuthenticatedCommandContext(
        principal=principal,
        grant=grant,
        workspace=workspace,
        contract=command_contract(CommandKind.TURN_START),
        envelope=envelope,
        authorized_at=NOW,
    )


def append_request(context: AuthenticatedCommandContext) -> AppendRequest:
    event = EventRecord(
        event_id=EVENT_ID,
        event_type="Turn.Accepted",
        schema_version="1.2",
        aggregate_id=TURN_ID,
        aggregate_sequence=1,
        actor_kind=EventActorKind.AUTHENTICATED,
        actor_principal_id=PRINCIPAL_ID,
        grant_id=context.grant.grant_id,
        policy_decision_id=identifier("dcs", 1),
        occurred_at=NOW,
        trace=TraceLink(
            request_id=identifier("req", 0),
            correlation_id=EVENT_ID,
        ),
        payload=payload("one logical turn"),
    )
    return AppendRequest(
        workspace_id=WORKSPACE_ID,
        aggregate_id=TURN_ID,
        expected_sequence=0,
        idempotency_key=IDEMPOTENCY_KEY,
        request_sha256=command_request_sha256(context.envelope),
        durability=JournalDurability.SYNCHRONOUS,
        events=(event,),
    )
