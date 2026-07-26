"""Deterministic append receipt construction shared by journal backends."""

import hashlib
import hmac
import json
from datetime import datetime

from app.services.harness.journal.contracts import (
    AppendRequest,
    AppendResult,
    AppendStatus,
)


def build_append_result(
    request: AppendRequest,
    committed_at: datetime,
    journal_sequences: tuple[int, ...],
) -> AppendResult:
    event_ids = tuple(event.event_id for event in request.events)
    first_sequence = request.events[0].aggregate_sequence
    last_sequence = request.events[-1].aggregate_sequence
    return AppendResult(
        workspace_id=request.workspace_id,
        aggregate_id=request.aggregate_id,
        status=AppendStatus.APPENDED,
        durability=request.durability,
        first_sequence=first_sequence,
        last_sequence=last_sequence,
        event_ids=event_ids,
        journal_sequences=journal_sequences,
        request_sha256=request.request_sha256,
        receipt_sha256=_receipt_sha256(
            workspace_id=request.workspace_id,
            aggregate_id=request.aggregate_id,
            durability=request.durability.value,
            event_ids=event_ids,
            first_sequence=first_sequence,
            last_sequence=last_sequence,
            request_sha256=request.request_sha256,
            committed_at=committed_at,
            journal_sequences=journal_sequences,
        ),
        committed_at=committed_at,
    )


def append_result_receipt_is_valid(result: AppendResult) -> bool:
    expected_sha256 = _receipt_sha256(
        workspace_id=result.workspace_id,
        aggregate_id=result.aggregate_id,
        durability=result.durability.value,
        event_ids=result.event_ids,
        first_sequence=result.first_sequence,
        last_sequence=result.last_sequence,
        request_sha256=result.request_sha256,
        committed_at=result.committed_at,
        journal_sequences=result.journal_sequences,
    )
    return hmac.compare_digest(expected_sha256, result.receipt_sha256)


def _receipt_sha256(
    *,
    workspace_id: str,
    aggregate_id: str,
    durability: str,
    event_ids: tuple[str, ...],
    first_sequence: int,
    last_sequence: int,
    request_sha256: str,
    committed_at: datetime,
    journal_sequences: tuple[int, ...],
) -> str:
    receipt_values = {
        "aggregate_id": aggregate_id,
        "workspace_id": workspace_id,
        "durability": durability,
        "event_ids": event_ids,
        "first_sequence": first_sequence,
        "last_sequence": last_sequence,
        "request_sha256": request_sha256,
        "committed_at": committed_at.isoformat(),
        "journal_sequences": journal_sequences,
    }
    receipt_bytes = json.dumps(
        receipt_values,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(receipt_bytes).hexdigest()
