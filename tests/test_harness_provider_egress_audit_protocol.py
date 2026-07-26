from datetime import UTC, datetime

from app.services.harness.protocol import (
    DataClassification,
    EgressAuditOutcome,
    ProviderEgressAuditRecord,
)
from app.services.harness.providers import (
    ProviderEgressAuditRecord as ProviderAuditExport,
)


def test_egress_audit_is_a_secret_free_shared_protocol_record() -> None:
    record = ProviderEgressAuditRecord(
        request_id="req_" + "1" * 32,
        provider="configured-provider",
        destination_sha256="2" * 64,
        target_url_sha256="3" * 64,
        classification=DataClassification.CONFIDENTIAL,
        body_sha256="4" * 64,
        inspection_policy_revision_sha256="5" * 64,
        body_bytes=128,
        attempt=1,
        redirect_count=0,
        outcome=EgressAuditOutcome.AUTHORIZED,
        reason="Provider request authorized.",
        recorded_at=datetime(2026, 7, 27, tzinfo=UTC),
    )

    serialized = record.model_dump_json()

    assert ProviderAuditExport is ProviderEgressAuditRecord
    assert "credential" not in serialized
    assert "target_url\"" not in serialized
    assert "authorization" not in serialized
    assert "prompt" not in serialized
