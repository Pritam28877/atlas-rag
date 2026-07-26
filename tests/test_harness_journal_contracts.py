import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services.harness.journal import (
    AppendRequest,
    AppendResult,
    AppendStatus,
    JournalConflictCode,
    JournalConflictError,
    JournalDurability,
    JournalPage,
    JournalReadRequest,
    raise_expected_sequence_conflict,
    raise_idempotency_conflict,
)
from app.services.harness.protocol import (
    EventActorKind,
    EventRecord,
    InlinePayload,
    TraceLink,
)

NOW = datetime(2026, 7, 26, 18, 0, tzinfo=UTC)


def identifier(prefix: str, number: int = 0) -> str:
    return f"{prefix}_{number:032x}"


def event(sequence: int, *, aggregate_number: int = 0) -> EventRecord:
    text = f"event-{sequence}"
    encoded = text.encode()
    return EventRecord(
        event_id=identifier("evt", sequence),
        event_type="Turn.Accepted",
        schema_version="1.2",
        aggregate_id=identifier("trn", aggregate_number),
        aggregate_sequence=sequence,
        actor_kind=EventActorKind.SYSTEM,
        actor_principal_id=identifier("prn"),
        occurred_at=NOW + timedelta(seconds=min(sequence, 100)),
        trace=TraceLink(
            request_id=identifier("req"),
            correlation_id=identifier("evt", 99),
        ),
        payload=InlinePayload(
            text=text,
            size_bytes=len(encoded),
            content_sha256=hashlib.sha256(encoded).hexdigest(),
        ),
    )


def request(*events: EventRecord, expected_sequence: int = 0) -> AppendRequest:
    return AppendRequest(
        aggregate_id=identifier("trn"),
        expected_sequence=expected_sequence,
        idempotency_key="journal-command-0001",
        request_sha256="1" * 64,
        events=events,
    )


def test_append_batch_is_aggregate_owned_contiguous_and_bounded() -> None:
    batch = request(event(1), event(2))

    assert batch.durability is JournalDurability.SYNCHRONOUS
    assert tuple(item.aggregate_sequence for item in batch.events) == (1, 2)

    with pytest.raises(ValidationError, match="another aggregate"):
        request(event(1, aggregate_number=1))
    with pytest.raises(ValidationError, match="contiguous"):
        request(event(2))
    duplicate_id_values = event(2).model_dump()
    duplicate_id_values["event_id"] = event(1).event_id
    duplicate_id_event = EventRecord.model_validate(duplicate_id_values)
    with pytest.raises(ValidationError, match="unique"):
        request(event(1), duplicate_id_event)


def test_append_batch_rejects_time_reversal_and_sequence_overflow() -> None:
    later_event = event(1)
    earlier_values = event(2).model_dump()
    earlier_values["occurred_at"] = NOW
    earlier_event = EventRecord.model_validate(earlier_values)
    with pytest.raises(ValidationError, match="nondecreasing"):
        request(later_event, earlier_event)

    maximum_sequence = 2**63 - 1
    with pytest.raises(ValidationError, match="signed 64-bit"):
        request(
            event(maximum_sequence),
            expected_sequence=maximum_sequence,
        )


def test_append_batch_serialized_size_is_hard_bounded() -> None:
    large_text = "a" * 32_768
    large_encoded = large_text.encode()
    events: list[EventRecord] = []
    for sequence in range(1, 129):
        values = event(sequence).model_dump()
        values["payload"] = InlinePayload(
            text=large_text,
            size_bytes=len(large_encoded),
            content_sha256=hashlib.sha256(large_encoded).hexdigest(),
        )
        events.append(EventRecord.model_validate(values))

    with pytest.raises(ValidationError, match="4 MiB"):
        request(*events)


def test_append_result_span_and_page_order_are_exact() -> None:
    result = AppendResult(
        aggregate_id=identifier("trn"),
        status=AppendStatus.APPENDED,
        durability=JournalDurability.SYNCHRONOUS,
        first_sequence=1,
        last_sequence=2,
        event_ids=(identifier("evt", 1), identifier("evt", 2)),
        request_sha256="1" * 64,
        receipt_sha256="2" * 64,
        committed_at=NOW,
    )
    assert result.last_sequence == 2

    page = JournalPage(
        aggregate_id=identifier("trn"),
        after_sequence=0,
        events=(event(1), event(2)),
        has_more=False,
    )
    assert len(page.events) == 2

    with pytest.raises(ValidationError, match="span"):
        AppendResult.model_validate(
            {
                **result.model_dump(),
                "last_sequence": 3,
            }
        )
    with pytest.raises(ValidationError, match="contiguous"):
        JournalPage(
            aggregate_id=identifier("trn"),
            after_sequence=0,
            events=(event(2),),
            has_more=False,
        )
    with pytest.raises(ValidationError, match="cannot be empty"):
        JournalPage(
            aggregate_id=identifier("trn"),
            after_sequence=0,
            events=(),
            has_more=True,
        )


@pytest.mark.parametrize("limit", (0, 257))
def test_read_request_is_bounded(limit: int) -> None:
    with pytest.raises(ValidationError):
        JournalReadRequest(
            aggregate_id=identifier("trn"),
            after_sequence=0,
            limit=limit,
        )


@pytest.mark.parametrize(
    ("raise_conflict", "expected_code"),
    (
        (
            raise_expected_sequence_conflict,
            JournalConflictCode.EXPECTED_SEQUENCE,
        ),
        (
            raise_idempotency_conflict,
            JournalConflictCode.IDEMPOTENCY_MISMATCH,
        ),
    ),
)
def test_conflicts_are_structured_without_storage_detail(
    raise_conflict: Callable[[int], None],
    expected_code: JournalConflictCode,
) -> None:
    with pytest.raises(JournalConflictError) as conflict:
        raise_conflict(7)
    assert conflict.value.code is expected_code
    assert conflict.value.current_sequence == 7
    assert str(conflict.value) == "journal append conflict"
