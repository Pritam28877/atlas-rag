"""Storage-neutral journal failures safe to expose at service boundaries."""


class JournalBusyError(RuntimeError):
    """A bounded journal queue has no capacity for another operation."""


class JournalStorageError(RuntimeError):
    """A journal backend failed without exposing storage internals."""


class RetentionStoreConflict(RuntimeError):
    """Requested immutable retention evidence conflicts with durable state."""
