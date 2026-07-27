"""Evidence ledger contracts and deterministic replay tests."""

import hashlib
import json

import pytest

from app.services.harness.documents import (
    DocumentSpan,
    EvidenceClaim,
    EvidenceClassification,
    EvidenceClassificationRecord,
    EvidenceEventType,
    EvidenceFinishOutcome,
    EvidenceFinishRecord,
    EvidenceLedgerEvent,
    EvidenceQuery,
    replay_evidence_ledger,
)
from app.services.harness.documents.ledger import _event_digest

TENANT = "ten_" + "1" * 32
WORKSPACE = "wsp_" + "2" * 32
SOURCE = "doc/source"
QUERY = "query/one"


def _span() -> DocumentSpan:
    text = "exact evidence"
    return DocumentSpan(
        document_id=SOURCE,
        revision_sha256="a" * 64,
        page_number=1,
        start_offset=0,
        end_offset=len(text),
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


def _event(
    event_type: EvidenceEventType,
    payload: object,
    sequence: int,
) -> EvidenceLedgerEvent:
    payload_json = json.dumps(
        payload.model_dump(mode="json"),
        separators=(",", ":"),
        sort_keys=True,
    )
    event = EvidenceLedgerEvent(
        sequence=sequence,
        event_type=event_type,
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        payload_json=payload_json,
        event_sha256="0" * 64,
    )
    return event.model_copy(update={"event_sha256": _event_digest(event)})


def test_ledger_replay_is_deterministic_and_quote_is_immutable() -> None:
    query = EvidenceQuery(
        query_id=QUERY,
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        text="what happened",
        retriever_revision="retriever.v1",
        tool_revision="tool.v1",
    )
    classification = EvidenceClassificationRecord(
        candidate_id="candidate/one",
        classification=EvidenceClassification.RELEVANT,
        reason="exact match",
    )
    claim = EvidenceClaim(
        claim_id="claim/one",
        text="It happened.",
        citation_ids=(),
    )
    finish = EvidenceFinishRecord(
        outcome=EvidenceFinishOutcome.UNSUPPORTED,
        claim_ids=("claim/one",),
        reason="citation required",
    )
    events = (
        _event(EvidenceEventType.QUERY_RECORDED, query, 1),
        _event(EvidenceEventType.CLASSIFICATION_RECORDED, classification, 2),
        _event(EvidenceEventType.CLAIM_RECORDED, claim, 3),
        _event(EvidenceEventType.FINISH_RECORDED, finish, 4),
    )
    assert replay_evidence_ledger(events) == replay_evidence_ledger(
        tuple(reversed(events))
    )
    with pytest.raises(ValueError):
        DocumentSpan.model_validate(_span().model_dump() | {"text": "tampered"})
