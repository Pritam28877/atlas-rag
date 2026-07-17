"""Time-bounded database snapshots for low-cardinality API metrics."""

from collections.abc import Mapping, Sequence

from sqlalchemy import text

from app.core.database import Database


async def read_database_metrics(
    database: Database,
    query_timeout_milliseconds: int,
) -> tuple[Sequence[Mapping[str, object]], object | None]:
    async with database.transaction() as session:
        await session.execute(
            text("SELECT set_config('statement_timeout', :timeout, true)"),
            {"timeout": f"{query_timeout_milliseconds}ms"},
        )
        rows = (
            await session.execute(
                text(
                    """
                    SELECT CASE
                             WHEN state = 'dead_lettered'
                               THEN 'dead-lettered-job'
                             WHEN stage IN ('preflight', 'native_parse')
                               THEN 'native'
                             WHEN stage = 'ocr' THEN 'ocr'
                             WHEN stage IN ('chunk', 'embed', 'index')
                               THEN 'publication'
                             WHEN stage = 'delete' THEN 'lifecycle'
                             ELSE 'unclassified'
                           END AS queue,
                           count(*) AS depth,
                           extract(epoch FROM now() - min(
                               COALESCE(retry_at, updated_at)
                           )) AS oldest_age
                    FROM ingestion_jobs
                    WHERE state IN (
                        'pending', 'retry_scheduled', 'dead_lettered'
                    )
                    GROUP BY 1
                    """
                )
            )
        ).mappings().all()
        heartbeat_age = await session.scalar(
            text(
                """
                SELECT extract(epoch FROM now() - updated_at)
                FROM service_heartbeats
                WHERE service_name = 'document-loader-reconciliation'
                """
            )
        )
    normalized_rows: list[dict[str, object]] = []
    for row in rows:
        normalized_rows.append({str(key): value for key, value in row.items()})
    return normalized_rows, heartbeat_age
