from dataclasses import dataclass, replace

from app.services.catalog.enums import VersionState


class CatalogTransitionError(ValueError):
    """Base class for deterministic catalog transition failures."""


class StaleTransitionError(CatalogTransitionError):
    """Raised when a worker acts on an obsolete state revision."""


class IllegalTransitionError(CatalogTransitionError):
    """Raised when the requested state edge violates the pipeline contract."""


@dataclass(frozen=True, slots=True)
class PublicationEvidence:
    chunk_count: int = 0
    embedded_chunk_count: int = 0
    lexical_publication_count: int = 0
    vector_publication_count: int = 0
    chunk_manifest_published: bool = False
    index_manifest_published: bool = False

    def is_complete(self) -> bool:
        return (
            self.chunk_count > 0
            and self.embedded_chunk_count == self.chunk_count
            and self.lexical_publication_count == self.chunk_count
            and self.vector_publication_count == self.chunk_count
            and self.chunk_manifest_published
            and self.index_manifest_published
        )


@dataclass(frozen=True, slots=True)
class VersionSnapshot:
    state: VersionState
    revision: int
    progress_completed: int = 0
    progress_total: int = 0
    terminal_reason_code: str | None = None


TERMINAL_STATES = frozenset(
    {
        VersionState.READY,
        VersionState.READY_WITH_WARNINGS,
        VersionState.DEDUPLICATED,
        VersionState.REJECTED,
        VersionState.FAILED,
        VersionState.QUARANTINED,
        VersionState.SUPERSEDED,
        VersionState.CANCELLED,
        VersionState.DELETED,
    }
)

_FORWARD_TRANSITIONS: dict[VersionState, frozenset[VersionState]] = {
    VersionState.RECEIVED: frozenset(
        {
            VersionState.VALIDATING,
            VersionState.CANCELLED,
            VersionState.DELETED,
        }
    ),
    VersionState.VALIDATING: frozenset(
        {
            VersionState.QUEUED,
            VersionState.DEDUPLICATED,
            VersionState.REJECTED,
            VersionState.FAILED,
            VersionState.QUARANTINED,
            VersionState.CANCELLED,
        }
    ),
    VersionState.QUEUED: frozenset(
        {VersionState.PARSING, VersionState.CANCELLED, VersionState.FAILED}
    ),
    VersionState.PARSING: frozenset(
        {
            VersionState.OCR,
            VersionState.NORMALIZING,
            VersionState.FAILED,
            VersionState.QUARANTINED,
            VersionState.CANCELLED,
        }
    ),
    VersionState.OCR: frozenset(
        {
            VersionState.NORMALIZING,
            VersionState.FAILED,
            VersionState.CANCELLED,
        }
    ),
    VersionState.NORMALIZING: frozenset(
        {VersionState.CHUNKING, VersionState.FAILED, VersionState.CANCELLED}
    ),
    VersionState.CHUNKING: frozenset(
        {VersionState.EMBEDDING, VersionState.FAILED, VersionState.CANCELLED}
    ),
    VersionState.EMBEDDING: frozenset(
        {VersionState.INDEXING, VersionState.FAILED, VersionState.CANCELLED}
    ),
    VersionState.INDEXING: frozenset(
        {
            VersionState.READY,
            VersionState.READY_WITH_WARNINGS,
            VersionState.FAILED,
            VersionState.CANCELLED,
        }
    ),
}

_DELETABLE_STATES = frozenset(set(VersionState) - {VersionState.DELETED})
_SUPERSEDEABLE_STATES = frozenset(
    {VersionState.READY, VersionState.READY_WITH_WARNINGS}
)
_REPROCESSABLE_STATES = frozenset(
    {
        VersionState.READY,
        VersionState.READY_WITH_WARNINGS,
        VersionState.FAILED,
        VersionState.CANCELLED,
    }
)


def transition_version(
    snapshot: VersionSnapshot,
    target_state: VersionState,
    *,
    expected_revision: int,
    progress_completed: int | None = None,
    progress_total: int | None = None,
    terminal_reason_code: str | None = None,
    publication_evidence: PublicationEvidence | None = None,
    operation: str = "advance",
) -> VersionSnapshot:
    """Validate one optimistic, monotonic document-version state transition."""
    if snapshot.revision != expected_revision:
        raise StaleTransitionError(
            f"expected revision {expected_revision}, found {snapshot.revision}"
        )

    next_completed = (
        snapshot.progress_completed
        if progress_completed is None
        else progress_completed
    )
    next_total = snapshot.progress_total if progress_total is None else progress_total
    _validate_progress(snapshot, next_completed, next_total)
    _validate_operation(snapshot.state, target_state, operation)
    _validate_terminal_reason(target_state, terminal_reason_code)

    if target_state in {VersionState.READY, VersionState.READY_WITH_WARNINGS}:
        if publication_evidence is None or not publication_evidence.is_complete():
            raise IllegalTransitionError(
                "ready state requires complete chunk, embedding, "
                "and publication evidence"
            )

    return replace(
        snapshot,
        state=target_state,
        revision=snapshot.revision + 1,
        progress_completed=next_completed,
        progress_total=next_total,
        terminal_reason_code=terminal_reason_code,
    )


def _validate_progress(
    snapshot: VersionSnapshot,
    progress_completed: int,
    progress_total: int,
) -> None:
    if progress_completed < snapshot.progress_completed:
        raise IllegalTransitionError("completed progress cannot decrease")
    if progress_total < snapshot.progress_total:
        raise IllegalTransitionError("total progress cannot decrease")
    if progress_completed < 0 or progress_total < 0:
        raise IllegalTransitionError("progress cannot be negative")
    if progress_total > 0 and progress_completed > progress_total:
        raise IllegalTransitionError("completed progress cannot exceed total progress")


def _validate_operation(
    current_state: VersionState,
    target_state: VersionState,
    operation: str,
) -> None:
    if operation == "delete":
        allowed = (
            current_state in _DELETABLE_STATES
            and target_state is VersionState.DELETED
        )
    elif operation == "supersede":
        allowed = (
            current_state in _SUPERSEDEABLE_STATES
            and target_state is VersionState.SUPERSEDED
        )
    elif operation == "reprocess":
        allowed = (
            current_state in _REPROCESSABLE_STATES
            and target_state is VersionState.QUEUED
        )
    elif operation == "cancel":
        allowed = (
            current_state not in TERMINAL_STATES
            and target_state is VersionState.CANCELLED
        )
    elif operation == "retry":
        allowed = (
            current_state is VersionState.FAILED
            and target_state is VersionState.QUEUED
        )
    elif operation == "advance":
        allowed = target_state in _FORWARD_TRANSITIONS.get(current_state, frozenset())
    else:
        raise IllegalTransitionError(f"unknown transition operation: {operation}")

    if not allowed:
        raise IllegalTransitionError(
            f"cannot {operation} from {current_state.value} to {target_state.value}"
        )


def _validate_terminal_reason(
    target_state: VersionState,
    terminal_reason_code: str | None,
) -> None:
    failure_states = {
        VersionState.DEDUPLICATED,
        VersionState.REJECTED,
        VersionState.FAILED,
        VersionState.QUARANTINED,
    }
    if target_state in failure_states and terminal_reason_code is None:
        raise IllegalTransitionError(
            "unsuccessful terminal state requires a reason code"
        )
    if target_state not in TERMINAL_STATES and terminal_reason_code is not None:
        raise IllegalTransitionError("non-terminal state cannot have a terminal reason")
