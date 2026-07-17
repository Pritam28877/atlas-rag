"""Durable bounded retry policy for publication stages."""

from app.core.config import Settings
from app.core.database import Database
from app.services.catalog.enums import VersionState
from app.services.ingestion.publication_models import ClaimedPublicationJob
from app.services.ingestion.publication_repository import PublicationJobRepository


def retry_delay(settings: Settings, job: ClaimedPublicationJob) -> int:
    delay = settings.workers.retry_base_seconds * (2 ** (job.attempt_number - 1))
    return min(delay, settings.workers.retry_max_seconds)


async def retry_or_fail(
    database: Database,
    repository: PublicationJobRepository,
    settings: Settings,
    job: ClaimedPublicationJob,
    worker_id: str,
) -> bool:
    async with database.transaction() as session:
        if job.attempt_number >= job.max_attempts:
            await repository.fail_permanently(
                session,
                job,
                worker_id,
                VersionState.FAILED,
                "PROCESSING_RETRY_EXHAUSTED",
            )
            return False
        await repository.schedule_retry(
            session,
            job,
            worker_id,
            "PUBLICATION_DEPENDENCY_UNAVAILABLE",
            retry_delay(settings, job),
        )
    return True
