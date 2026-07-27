"""Deterministic append-only evidence ledger reducer."""

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import Field

from app.services.harness.documents.contracts import (
    MAXIMUM_EVIDENCE_RECORDS,
    DocumentVersion,
    EvidenceCandidate,
    EvidenceCitation,
    EvidenceClaim,
    EvidenceClassificationRecord,
    EvidenceFinishRecord,
    EvidenceLedgerSnapshot,
    EvidenceQuery,
)
from app.services.harness.protocol.base import (
    Sha256,
    StrictProtocolModel,
    TenantId,
    WorkspaceId,
)
from app.services.harness.protocol.conversation import SourceId

type EvidenceRecord = (
    DocumentVersion
    | EvidenceQuery
    | EvidenceCandidate
    | EvidenceClassificationRecord
    | EvidenceCitation
    | EvidenceClaim
    | EvidenceFinishRecord
)


class EvidenceEventType(StrEnum):
    SOURCE_REGISTERED = "source_registered"
    QUERY_RECORDED = "query_recorded"
    CANDIDATE_RECORDED = "candidate_recorded"
    CLASSIFICATION_RECORDED = "classification_recorded"
    CITATION_RECORDED = "citation_recorded"
    CLAIM_RECORDED = "claim_recorded"
    FINISH_RECORDED = "finish_recorded"


class EvidenceLedgerEvent(StrictProtocolModel):
    sequence: int = Field(ge=1, le=MAXIMUM_EVIDENCE_RECORDS)
    event_type: EvidenceEventType
    tenant_id: TenantId
    workspace_id: WorkspaceId
    payload_json: str = Field(min_length=2, max_length=512 * 1024)
    event_sha256: Sha256


class EvidenceLedgerError(ValueError):
    pass


def replay_evidence_ledger(
    events: tuple[EvidenceLedgerEvent, ...],
) -> EvidenceLedgerSnapshot:
    if len(events) > MAXIMUM_EVIDENCE_RECORDS:
        raise EvidenceLedgerError("evidence event count exceeds limit")
    ordered = tuple(sorted(events, key=lambda event: event.sequence))
    if tuple(event.sequence for event in ordered) != tuple(range(1, len(ordered) + 1)):
        raise EvidenceLedgerError("evidence sequences must be contiguous")
    snapshot = EvidenceLedgerSnapshot(
        sources=(),
        queries=(),
        candidates=(),
        classifications=(),
        citations=(),
        claims=(),
    )
    for event in ordered:
        verified = EvidenceLedgerEvent.model_validate(event.model_dump())
        if _event_digest(verified) != verified.event_sha256:
            raise EvidenceLedgerError("evidence event hash is invalid")
        _parse_payload(verified.payload_json)
        snapshot = _apply_event(snapshot, verified, verified.payload_json)
    return snapshot


def _apply_event(
    snapshot: EvidenceLedgerSnapshot,
    event: EvidenceLedgerEvent,
    payload_json: str,
) -> EvidenceLedgerSnapshot:
    record = _parse_record(event.event_type, payload_json)
    _validate_scope(record, event)
    values = snapshot.model_dump()
    field = {
        EvidenceEventType.SOURCE_REGISTERED: "sources",
        EvidenceEventType.QUERY_RECORDED: "queries",
        EvidenceEventType.CANDIDATE_RECORDED: "candidates",
        EvidenceEventType.CLASSIFICATION_RECORDED: "classifications",
        EvidenceEventType.CITATION_RECORDED: "citations",
        EvidenceEventType.CLAIM_RECORDED: "claims",
        EvidenceEventType.FINISH_RECORDED: "finish",
    }[event.event_type]
    if field == "finish":
        values[field] = record.model_dump()
    else:
        values[field] = (*values[field], record.model_dump())
    return EvidenceLedgerSnapshot.model_validate(values)


def _validate_scope(record: object, event: EvidenceLedgerEvent) -> None:
    if hasattr(record, "tenant_id") and getattr(record, "tenant_id") != event.tenant_id:
        raise EvidenceLedgerError("evidence tenant scope changed")
    if (
        hasattr(record, "workspace_id")
        and getattr(record, "workspace_id") != event.workspace_id
    ):
        raise EvidenceLedgerError("evidence workspace scope changed")
    if isinstance(record, EvidenceCandidate):
        _ensure_record_scope(record.span.document_id, event)
    if isinstance(record, EvidenceCitation):
        _ensure_record_scope(record.span.document_id, event)


def _ensure_record_scope(_: SourceId, __: EvidenceLedgerEvent) -> None:
    return


def _parse_record(
    event_type: EvidenceEventType,
    payload_json: str,
) -> EvidenceRecord:
    if event_type is EvidenceEventType.SOURCE_REGISTERED:
        return DocumentVersion.model_validate_json(payload_json)
    if event_type is EvidenceEventType.QUERY_RECORDED:
        return EvidenceQuery.model_validate_json(payload_json)
    if event_type is EvidenceEventType.CANDIDATE_RECORDED:
        return EvidenceCandidate.model_validate_json(payload_json)
    if event_type is EvidenceEventType.CLASSIFICATION_RECORDED:
        return EvidenceClassificationRecord.model_validate_json(payload_json)
    if event_type is EvidenceEventType.CITATION_RECORDED:
        return EvidenceCitation.model_validate_json(payload_json)
    if event_type is EvidenceEventType.CLAIM_RECORDED:
        return EvidenceClaim.model_validate_json(payload_json)
    return EvidenceFinishRecord.model_validate_json(payload_json)


def _parse_payload(payload_json: str) -> dict[str, object]:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise EvidenceLedgerError("evidence payload is invalid JSON") from error
    if not isinstance(payload, dict):
        raise EvidenceLedgerError("evidence payload must be an object")
    return payload


def _event_digest(event: EvidenceLedgerEvent) -> str:
    values = event.model_dump(mode="json", exclude={"event_sha256"})
    import hashlib

    return hashlib.sha256(
        json.dumps(
            values,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
