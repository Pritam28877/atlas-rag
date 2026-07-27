"""Crash-retryable release and deletion of expired background jobs."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from pydantic import Field

from app.services.harness.protocol import StrictProtocolModel
from app.services.harness.protocol.background_jobs import BackgroundJobRecord


class BackgroundJobRetentionStore(Protocol):
    async def load_expired(
        self,
        *,
        expired_at_or_before: datetime,
        maximum_records: int,
    ) -> tuple[BackgroundJobRecord, ...]: ...

    async def delete_expired(
        self,
        job: BackgroundJobRecord,
        *,
        expired_at_or_before: datetime,
    ) -> bool: ...


class BackgroundJobCleanupReport(StrictProtocolModel):
    jobs_checked: int = Field(ge=0, le=256)
    jobs_deleted: int = Field(ge=0, le=256)


class BackgroundJobRetentionCleaner:
    def __init__(
        self,
        job_store: BackgroundJobRetentionStore,
    ) -> None:
        self._job_store = job_store

    async def clean(
        self,
        *,
        observed_at: datetime,
        maximum_records: int = 128,
    ) -> BackgroundJobCleanupReport:
        _require_utc(observed_at)
        if not 1 <= maximum_records <= 256:
            raise ValueError("background cleanup batch must be between 1 and 256")
        jobs = await self._job_store.load_expired(
            expired_at_or_before=observed_at,
            maximum_records=maximum_records,
        )
        deleted = 0
        for job in jobs:
            removed = await self._job_store.delete_expired(
                job,
                expired_at_or_before=observed_at,
            )
            deleted += int(removed)
        return BackgroundJobCleanupReport(
            jobs_checked=len(jobs),
            jobs_deleted=deleted,
        )


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("background cleanup timestamp must use UTC")
