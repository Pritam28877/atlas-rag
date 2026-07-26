"""Bounded restartable catch-up and all-or-nothing projection rebuild."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from app.services.harness.journal.contracts import (
    EventJournal,
    GlobalJournalPage,
    GlobalJournalReadRequest,
    JournalEvent,
)
from app.services.harness.journal.projection_contracts import (
    ProjectionCheckpoint,
    ProjectionDefinition,
    ProjectionError,
    StateType,
)
from app.services.harness.journal.projection_engine import (
    advance_projection,
    initial_checkpoint,
    require_equivalent_projection,
)
from app.services.harness.journal.projection_store import (
    ProjectionHealth,
    ProjectionStore,
    ProjectionStoreConflict,
    StoredProjection,
    projection_expectation,
)
from app.services.harness.protocol import StrictProtocolModel, WorkspaceId

MAXIMUM_RUN_PAGES = 10_000


class ProjectionRunErrorCode(StrEnum):
    CONFLICT = "conflict"
    DIVERGENCE = "divergence"
    REBUILD_LIMIT = "rebuild_limit"
    UNHEALTHY = "unhealthy"


class ProjectionRunError(RuntimeError):
    def __init__(self, code: ProjectionRunErrorCode) -> None:
        super().__init__("projection run failed")
        self.code = code


class ProjectionRunResult(StrictProtocolModel):
    checkpoint: ProjectionCheckpoint
    generation: int | None = Field(default=None, ge=1, le=2**63 - 1)
    pages_processed: int = Field(ge=0, le=MAXIMUM_RUN_PAGES)
    has_more: bool


class ProjectionRunner:
    def __init__(
        self,
        journal: EventJournal,
        store: ProjectionStore,
        *,
        page_size: int = 256,
    ) -> None:
        if not 1 <= page_size <= 256:
            raise ValueError("projection page size must be between 1 and 256")
        self._journal = journal
        self._store = store
        self._page_size = page_size

    async def catch_up(
        self,
        definition: ProjectionDefinition[StateType],
        workspace_id: WorkspaceId,
        *,
        maximum_pages: int = 128,
    ) -> ProjectionRunResult:
        self._validate_maximum_pages(maximum_pages)
        stored = await self._store.load(workspace_id, definition.name)
        if stored is not None and stored.health is not ProjectionHealth.HEALTHY:
            raise ProjectionRunError(ProjectionRunErrorCode.UNHEALTHY)
        checkpoint = (
            stored.checkpoint
            if stored is not None
            else initial_checkpoint(definition, workspace_id)
        )
        pages_processed = 0
        has_more = False
        while pages_processed < maximum_pages:
            page = await self._read_page(workspace_id, checkpoint)
            if not page.events:
                has_more = False
                break
            next_checkpoint = advance_projection(
                definition,
                workspace_id,
                page.events,
                checkpoint=checkpoint,
            )
            try:
                stored = await self._store.save_online(
                    next_checkpoint,
                    projection_expectation(stored),
                )
            except ProjectionStoreConflict as error:
                raise ProjectionRunError(ProjectionRunErrorCode.CONFLICT) from error
            checkpoint = stored.checkpoint
            pages_processed += 1
            has_more = page.has_more
            if not has_more:
                break
        return ProjectionRunResult(
            checkpoint=checkpoint,
            generation=None if stored is None else stored.generation,
            pages_processed=pages_processed,
            has_more=has_more,
        )

    async def rebuild(
        self,
        definition: ProjectionDefinition[StateType],
        workspace_id: WorkspaceId,
        *,
        maximum_pages: int = 1_024,
    ) -> ProjectionRunResult:
        self._validate_maximum_pages(maximum_pages)
        baseline = await self._store.load(workspace_id, definition.name)
        checkpoint = initial_checkpoint(definition, workspace_id)
        baseline_verified = (
            baseline is None
            or baseline.health is not ProjectionHealth.HEALTHY
        )
        if baseline is not None and baseline.checkpoint.last_journal_sequence == 0:
            if not self._baseline_matches(checkpoint, baseline):
                await self._record_divergence(baseline)
            baseline_verified = True

        pages_processed = 0
        has_more = False
        while pages_processed < maximum_pages:
            page = await self._read_page(workspace_id, checkpoint)
            if not page.events:
                has_more = False
                break
            checkpoint, baseline_verified = self._advance_rebuild_page(
                definition,
                workspace_id,
                checkpoint,
                page.events,
                baseline,
                baseline_verified,
            )
            pages_processed += 1
            has_more = page.has_more
            if not has_more:
                break
        if has_more:
            raise ProjectionRunError(ProjectionRunErrorCode.REBUILD_LIMIT)
        if not baseline_verified:
            await self._record_divergence(baseline)
        try:
            stored = await self._store.replace_rebuild(
                checkpoint,
                projection_expectation(baseline),
            )
        except ProjectionStoreConflict as error:
            raise ProjectionRunError(ProjectionRunErrorCode.CONFLICT) from error
        return ProjectionRunResult(
            checkpoint=stored.checkpoint,
            generation=stored.generation,
            pages_processed=pages_processed,
            has_more=False,
        )

    async def _read_page(
        self,
        workspace_id: WorkspaceId,
        checkpoint: ProjectionCheckpoint,
    ) -> GlobalJournalPage:
        return await self._journal.read_global(
            GlobalJournalReadRequest(
                workspace_id=workspace_id,
                after_journal_sequence=checkpoint.last_journal_sequence,
                limit=self._page_size,
            )
        )

    def _advance_rebuild_page(
        self,
        definition: ProjectionDefinition[StateType],
        workspace_id: WorkspaceId,
        checkpoint: ProjectionCheckpoint,
        events: tuple[JournalEvent, ...],
        baseline: StoredProjection | None,
        baseline_verified: bool,
    ) -> tuple[ProjectionCheckpoint, bool]:
        if baseline is None or baseline_verified:
            return (
                advance_projection(
                    definition,
                    workspace_id,
                    events,
                    checkpoint=checkpoint,
                ),
                baseline_verified,
            )
        baseline_sequence = baseline.checkpoint.last_journal_sequence
        prefix_length = 0
        for journal_event in events:
            if journal_event.journal_sequence <= baseline_sequence:
                prefix_length += 1
        if prefix_length:
            checkpoint = advance_projection(
                definition,
                workspace_id,
                events[:prefix_length],
                checkpoint=checkpoint,
            )
        if checkpoint.last_journal_sequence == baseline_sequence:
            baseline_verified = self._baseline_matches(checkpoint, baseline)
        remaining_events = events[prefix_length:]
        if remaining_events:
            checkpoint = advance_projection(
                definition,
                workspace_id,
                remaining_events,
                checkpoint=checkpoint,
            )
        return checkpoint, baseline_verified

    async def _record_divergence(
        self,
        baseline: StoredProjection | None,
    ) -> None:
        if baseline is not None and baseline.health is ProjectionHealth.HEALTHY:
            try:
                await self._store.mark_unhealthy(
                    baseline.checkpoint.workspace_id,
                    baseline.checkpoint.projection_name,
                    ProjectionHealth.DIVERGED,
                    "replay_divergence",
                    projection_expectation(baseline),
                )
            except ProjectionStoreConflict as error:
                raise ProjectionRunError(ProjectionRunErrorCode.CONFLICT) from error
        raise ProjectionRunError(ProjectionRunErrorCode.DIVERGENCE)

    @staticmethod
    def _baseline_matches(
        rebuilt: ProjectionCheckpoint,
        baseline: StoredProjection,
    ) -> bool:
        try:
            require_equivalent_projection(baseline.checkpoint, rebuilt)
        except ProjectionError:
            return False
        return True

    @staticmethod
    def _validate_maximum_pages(maximum_pages: int) -> None:
        if not 1 <= maximum_pages <= MAXIMUM_RUN_PAGES:
            raise ValueError(
                f"maximum projection pages must be between 1 and {MAXIMUM_RUN_PAGES}"
            )
