"""Content-addressed journal persistence for provider egress audit facts."""

from __future__ import annotations

import hashlib

from pydantic import Field

from app.services.harness.journal.contracts import (
    AppendRequest,
    EventJournal,
)
from app.services.harness.protocol import (
    CURRENT_SCHEMA_VERSION,
    CanonicalEventType,
    DataClassification,
    EgressAuditOutcome,
    EventActorKind,
    EventRecord,
    InlinePayload,
    PrincipalId,
    ProviderEgressAuditRecord,
    ProviderName,
    RequestId,
    Sha256,
    StrictProtocolModel,
    TraceLink,
    UtcTimestamp,
    WorkspaceId,
)


class _DurableEgressAuditPayload(StrictProtocolModel):
    request_id: RequestId
    provider: ProviderName
    destination_sha256: Sha256
    target_url_sha256: Sha256
    classification: DataClassification
    body_sha256: Sha256
    inspection_policy_revision_sha256: Sha256
    body_bytes: int = Field(ge=1, le=16 * 1024 * 1024)
    attempt: int = Field(ge=1, le=64)
    redirect_count: int = Field(ge=0, le=5)
    outcome: EgressAuditOutcome
    response_status: int | None = Field(default=None, ge=100, le=599)
    response_bytes: int | None = Field(
        default=None,
        ge=0,
        le=16 * 1024 * 1024,
    )
    reason_sha256: Sha256
    recorded_at: UtcTimestamp


class JournalProviderEgressAuditSink:
    """Persists one immutable aggregate per redacted provider audit fact."""

    def __init__(
        self,
        journal: EventJournal,
        *,
        workspace_id: WorkspaceId,
        actor_principal_id: PrincipalId,
    ) -> None:
        self._journal = journal
        self._workspace_id = workspace_id
        self._actor_principal_id = actor_principal_id

    async def record(self, record: ProviderEgressAuditRecord) -> None:
        record_content = record.model_dump_json().encode()
        record_sha256 = hashlib.sha256(record_content).hexdigest()
        payload_text = _payload(record).model_dump_json()
        payload_content = payload_text.encode()
        event = EventRecord(
            event_id=f"evt_{record_sha256[:32]}",
            event_type=_event_type(record.outcome),
            schema_version=CURRENT_SCHEMA_VERSION,
            aggregate_id=f"opn_{record_sha256[:32]}",
            aggregate_sequence=1,
            actor_kind=EventActorKind.SYSTEM,
            actor_principal_id=self._actor_principal_id,
            occurred_at=record.recorded_at,
            trace=TraceLink(
                request_id=record.request_id,
                correlation_id=_correlation_id(record.request_id),
            ),
            payload=InlinePayload(
                media_type="application/json",
                text=payload_text,
                size_bytes=len(payload_content),
                content_sha256=hashlib.sha256(payload_content).hexdigest(),
            ),
        )
        event_sha256 = hashlib.sha256(
            event.model_dump_json().encode()
        ).hexdigest()
        await self._journal.append(
            AppendRequest(
                workspace_id=self._workspace_id,
                aggregate_id=event.aggregate_id,
                expected_sequence=0,
                idempotency_key=f"provider-egress-{record_sha256[:32]}",
                request_sha256=event_sha256,
                events=(event,),
            )
        )


def _payload(record: ProviderEgressAuditRecord) -> _DurableEgressAuditPayload:
    values = record.model_dump(exclude={"reason"})
    values["reason_sha256"] = hashlib.sha256(record.reason.encode()).hexdigest()
    return _DurableEgressAuditPayload.model_validate(values)


def _correlation_id(request_id: str) -> str:
    request_sha256 = hashlib.sha256(request_id.encode()).hexdigest()
    return f"evt_{request_sha256[:32]}"


def _event_type(outcome: EgressAuditOutcome) -> CanonicalEventType:
    if outcome is EgressAuditOutcome.AUTHORIZED:
        return CanonicalEventType.PROVIDER_ATTEMPT_STARTED
    if outcome is EgressAuditOutcome.SUCCEEDED:
        return CanonicalEventType.PROVIDER_ATTEMPT_COMPLETED
    return CanonicalEventType.PROVIDER_ATTEMPT_FAILED
