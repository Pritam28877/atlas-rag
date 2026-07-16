import pytest

from app.services.catalog import (
    IllegalTransitionError,
    PublicationEvidence,
    StaleTransitionError,
    VersionSnapshot,
    VersionState,
    transition_version,
)


def snapshot(
    state: VersionState,
    *,
    revision: int = 0,
    completed: int = 0,
    total: int = 0,
) -> VersionSnapshot:
    return VersionSnapshot(
        state=state,
        revision=revision,
        progress_completed=completed,
        progress_total=total,
    )


def complete_publication(chunk_count: int = 3) -> PublicationEvidence:
    return PublicationEvidence(
        chunk_count=chunk_count,
        embedded_chunk_count=chunk_count,
        lexical_publication_count=chunk_count,
        vector_publication_count=chunk_count,
        chunk_manifest_published=True,
        index_manifest_published=True,
    )


@pytest.mark.parametrize(
    ("current_state", "target_state"),
    [
        (VersionState.RECEIVED, VersionState.VALIDATING),
        (VersionState.VALIDATING, VersionState.QUEUED),
        (VersionState.QUEUED, VersionState.PARSING),
        (VersionState.PARSING, VersionState.OCR),
        (VersionState.PARSING, VersionState.NORMALIZING),
        (VersionState.OCR, VersionState.NORMALIZING),
        (VersionState.NORMALIZING, VersionState.CHUNKING),
        (VersionState.CHUNKING, VersionState.EMBEDDING),
        (VersionState.EMBEDDING, VersionState.INDEXING),
    ],
)
def test_pipeline_forward_edges_are_legal(
    current_state: VersionState,
    target_state: VersionState,
) -> None:
    transitioned = transition_version(
        snapshot(current_state),
        target_state,
        expected_revision=0,
    )

    assert transitioned.state is target_state
    assert transitioned.revision == 1


def test_ready_requires_complete_durable_publication_evidence() -> None:
    current = snapshot(VersionState.INDEXING, revision=8, completed=3, total=3)

    transitioned = transition_version(
        current,
        VersionState.READY,
        expected_revision=8,
        publication_evidence=complete_publication(),
    )

    assert transitioned.state is VersionState.READY


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        PublicationEvidence(),
        PublicationEvidence(3, 2, 3, 3),
        PublicationEvidence(3, 3, 2, 3),
        PublicationEvidence(3, 3, 3, 2),
        PublicationEvidence(3, 3, 3, 3, True, False),
    ],
)
def test_ready_rejects_incomplete_publication_evidence(
    evidence: PublicationEvidence | None,
) -> None:
    with pytest.raises(IllegalTransitionError, match="publication evidence"):
        transition_version(
            snapshot(VersionState.INDEXING),
            VersionState.READY,
            expected_revision=0,
            publication_evidence=evidence,
        )


def test_stale_duplicate_delivery_fails_before_state_change() -> None:
    with pytest.raises(StaleTransitionError, match="expected revision 4, found 5"):
        transition_version(
            snapshot(VersionState.PARSING, revision=5),
            VersionState.NORMALIZING,
            expected_revision=4,
        )


def test_illegal_stage_skip_fails_deterministically() -> None:
    with pytest.raises(IllegalTransitionError, match="cannot advance"):
        transition_version(
            snapshot(VersionState.RECEIVED),
            VersionState.INDEXING,
            expected_revision=0,
        )


def test_progress_counters_cannot_move_backwards() -> None:
    with pytest.raises(
        IllegalTransitionError,
        match="completed progress cannot decrease",
    ):
        transition_version(
            snapshot(VersionState.PARSING, completed=2, total=4),
            VersionState.NORMALIZING,
            expected_revision=0,
            progress_completed=1,
            progress_total=4,
        )


@pytest.mark.parametrize(
    "target_state",
    [
        VersionState.DEDUPLICATED,
        VersionState.REJECTED,
        VersionState.FAILED,
        VersionState.QUARANTINED,
    ],
)
def test_unsuccessful_terminal_outcome_requires_reason(
    target_state: VersionState,
) -> None:
    with pytest.raises(IllegalTransitionError, match="requires a reason code"):
        transition_version(
            snapshot(VersionState.VALIDATING),
            target_state,
            expected_revision=0,
        )


def test_retry_requeues_failed_version() -> None:
    transitioned = transition_version(
        snapshot(VersionState.FAILED, revision=10),
        VersionState.QUEUED,
        expected_revision=10,
        operation="retry",
    )

    assert transitioned.state is VersionState.QUEUED
    assert transitioned.terminal_reason_code is None


@pytest.mark.parametrize(
    "state",
    [VersionState.READY, VersionState.READY_WITH_WARNINGS, VersionState.FAILED],
)
def test_reprocess_requeues_supported_terminal_state(state: VersionState) -> None:
    transitioned = transition_version(
        snapshot(state),
        VersionState.QUEUED,
        expected_revision=0,
        operation="reprocess",
    )

    assert transitioned.state is VersionState.QUEUED


def test_active_version_can_be_cancelled() -> None:
    transitioned = transition_version(
        snapshot(VersionState.OCR),
        VersionState.CANCELLED,
        expected_revision=0,
        operation="cancel",
    )

    assert transitioned.state is VersionState.CANCELLED


def test_ready_version_can_be_superseded() -> None:
    transitioned = transition_version(
        snapshot(VersionState.READY),
        VersionState.SUPERSEDED,
        expected_revision=0,
        operation="supersede",
    )

    assert transitioned.state is VersionState.SUPERSEDED


def test_any_non_deleted_version_can_be_deleted() -> None:
    transitioned = transition_version(
        snapshot(VersionState.QUARANTINED),
        VersionState.DELETED,
        expected_revision=0,
        operation="delete",
    )

    assert transitioned.state is VersionState.DELETED
