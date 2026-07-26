"""Deterministic append receipt construction shared by journal backends."""

import hashlib
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
    receipt_values = {
        "aggregate_id": request.aggregate_id,
        "workspace_id": request.workspace_id,
        "durability": request.durability.value,
        "event_ids": event_ids,
        "first_sequence": request.events[0].aggregate_sequence,
        "last_sequence": request.events[-1].aggregate_sequence,
        "request_sha256": request.request_sha256,
        "committed_at": committed_at.isoformat(),
        "journal_sequences": journal_sequences,
    }
    receipt_bytes = json.dumps(
        receipt_values,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return AppendResult(
        workspace_id=request.workspace_id,
        aggregate_id=request.aggregate_id,
        status=AppendStatus.APPENDED,
        durability=request.durability,
        first_sequence=request.events[0].aggregate_sequence,
        last_sequence=request.events[-1].aggregate_sequence,
        event_ids=event_ids,
        journal_sequences=journal_sequences,
        request_sha256=request.request_sha256,
        receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
        committed_at=committed_at,
    )
