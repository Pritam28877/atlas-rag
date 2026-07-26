"""Durable Atlas Harness event-journal adapters."""

from app.services.harness.journal.contracts import (
    AppendRequest,
    AppendResult,
    AppendStatus,
    EventJournal,
    GlobalJournalPage,
    GlobalJournalReadRequest,
    JournalConflictCode,
    JournalConflictError,
    JournalDurability,
    JournalEvent,
    JournalPage,
    JournalReadRequest,
    raise_expected_sequence_conflict,
    raise_idempotency_conflict,
)
from app.services.harness.journal.errors import (
    JournalBusyError,
    JournalStorageError,
)
from app.services.harness.journal.health import (
    JournalCorruptionCode,
    JournalCorruptionError,
    JournalHealthRecord,
    JournalHealthStatus,
    JournalVerificationResult,
)
from app.services.harness.journal.sqlite import SQLiteEventJournal
from app.services.harness.journal.sqlite_integrity import (
    SQLiteJournalIntegrityVerifier,
)

__all__ = (
    "AppendRequest",
    "AppendResult",
    "AppendStatus",
    "EventJournal",
    "GlobalJournalPage",
    "GlobalJournalReadRequest",
    "JournalConflictCode",
    "JournalConflictError",
    "JournalCorruptionCode",
    "JournalCorruptionError",
    "JournalDurability",
    "JournalEvent",
    "JournalHealthRecord",
    "JournalHealthStatus",
    "JournalPage",
    "JournalReadRequest",
    "JournalBusyError",
    "JournalStorageError",
    "JournalVerificationResult",
    "SQLiteEventJournal",
    "SQLiteJournalIntegrityVerifier",
    "raise_expected_sequence_conflict",
    "raise_idempotency_conflict",
)
