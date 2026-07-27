"""Storage-neutral journal failures safe to expose at service boundaries."""

from enum import StrEnum


class JournalBusyError(RuntimeError):
    """A bounded journal queue has no capacity for another operation."""


class JournalStorageError(RuntimeError):
    """A journal backend failed without exposing storage internals."""


class ApprovalStoreConflictCode(StrEnum):
    CAPACITY = "capacity"
    CONCURRENT_TRANSITION = "concurrent_transition"
    IDENTITY = "identity"
    OWNER = "owner"


class ApprovalStoreConflict(RuntimeError):
    """An approval write conflicts with durable immutable evidence."""

    def __init__(self, code: ApprovalStoreConflictCode) -> None:
        super().__init__("approval durability operation rejected")
        self.code = code


class RetentionStoreConflict(RuntimeError):
    """Requested immutable retention evidence conflicts with durable state."""


class RecoveryStoreConflict(RuntimeError):
    """Requested recovery fact conflicts with durable recovery evidence."""


class ProviderCostLedgerConflictCode(StrEnum):
    IDENTITY = "identity"
    RECONCILIATION = "reconciliation"
    STATE = "state"


class ProviderCostLedgerConflict(RuntimeError):
    """A cost reservation conflicts with durable accounting evidence."""

    def __init__(self, code: ProviderCostLedgerConflictCode) -> None:
        super().__init__("provider cost ledger operation rejected")
        self.code = code


class SessionStoreConflictCode(StrEnum):
    CAPACITY = "capacity"
    CURSOR_CONFLICT = "cursor_conflict"
    IDEMPOTENCY_MISMATCH = "idempotency_mismatch"
    OWNER_MISMATCH = "owner_mismatch"
    RESULT_CONFLICT = "result_conflict"


class SessionStoreConflict(RuntimeError):
    """A replay key or cursor conflicts with durable session ownership."""

    def __init__(self, code: SessionStoreConflictCode) -> None:
        super().__init__("session durability operation rejected")
        self.code = code
