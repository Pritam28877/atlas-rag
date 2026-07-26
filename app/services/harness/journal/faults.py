"""Explicit transaction boundaries available to durability test probes."""

from collections.abc import Awaitable, Callable
from enum import StrEnum


class JournalFaultPoint(StrEnum):
    AFTER_EVENTS_INSERTED = "after_events_inserted"
    AFTER_AGGREGATE_UPDATED = "after_aggregate_updated"
    AFTER_PROJECTIONS_APPLIED = "after_projections_applied"
    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT = "after_commit"


type AsyncJournalFaultInjector = Callable[
    [JournalFaultPoint],
    Awaitable[None] | None,
]
