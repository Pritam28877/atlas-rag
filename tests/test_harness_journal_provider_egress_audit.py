import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from app.services.harness.journal import (
    GlobalJournalReadRequest,
    JournalProviderEgressAuditSink,
    SQLiteEventJournal,
)
from app.services.harness.protocol import (
    CanonicalEventType,
    DataClassification,
    EgressAuditOutcome,
    InlinePayload,
    ProviderEgressAuditRecord,
)

CANARY = "provider-secret-canary-must-not-persist"
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
WORKSPACE_ID = "wsp_" + "1" * 32
PRINCIPAL_ID = "prn_" + "2" * 32


def audit_record(
    outcome: EgressAuditOutcome,
) -> ProviderEgressAuditRecord:
    succeeded = outcome is EgressAuditOutcome.SUCCEEDED
    return ProviderEgressAuditRecord(
        request_id="req_" + "3" * 32,
        provider="configured-provider",
        destination_sha256="4" * 64,
        target_url_sha256="5" * 64,
        classification=DataClassification.CONFIDENTIAL,
        body_sha256="6" * 64,
        inspection_policy_revision_sha256="7" * 64,
        body_bytes=128,
        attempt=1,
        redirect_count=0,
        outcome=outcome,
        response_status=200 if succeeded else None,
        response_bytes=16 if succeeded else None,
        reason=f"bounded reason containing {CANARY}",
        recorded_at=NOW,
    )


def test_audit_sink_is_durable_idempotent_and_secret_free(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        os.chmod(tmp_path, 0o700)
        database_path = tmp_path / "provider-audit.sqlite3"
        journal = await SQLiteEventJournal.open(database_path)
        try:
            sink = JournalProviderEgressAuditSink(
                journal,
                workspace_id=WORKSPACE_ID,
                actor_principal_id=PRINCIPAL_ID,
            )
            authorized = audit_record(EgressAuditOutcome.AUTHORIZED)
            await asyncio.gather(
                sink.record(authorized),
                sink.record(authorized),
            )
            await sink.record(audit_record(EgressAuditOutcome.SUCCEEDED))
            await sink.record(audit_record(EgressAuditOutcome.FAILED))
        finally:
            await journal.close()

        reopened = await SQLiteEventJournal.open(database_path)
        try:
            replay_sink = JournalProviderEgressAuditSink(
                reopened,
                workspace_id=WORKSPACE_ID,
                actor_principal_id=PRINCIPAL_ID,
            )
            await replay_sink.record(authorized)
            page = await reopened.read_global(
                GlobalJournalReadRequest(
                    workspace_id=WORKSPACE_ID,
                    after_journal_sequence=0,
                    limit=10,
                )
            )
        finally:
            await reopened.close()

        assert not page.has_more
        assert len(page.events) == 3
        assert [journal_event.event.event_type for journal_event in page.events] == [
            CanonicalEventType.PROVIDER_ATTEMPT_STARTED,
            CanonicalEventType.PROVIDER_ATTEMPT_COMPLETED,
            CanonicalEventType.PROVIDER_ATTEMPT_FAILED,
        ]
        serialized_events = " ".join(
            journal_event.event.model_dump_json()
            for journal_event in page.events
        )
        assert CANARY not in serialized_events
        for journal_event in page.events:
            payload = journal_event.event.payload
            assert isinstance(payload, InlinePayload)
            payload_document = json.loads(payload.text)
            assert "reason" not in payload_document
            assert len(payload_document["reason_sha256"]) == 64

    asyncio.run(scenario())
