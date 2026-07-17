"""Bounded local control-plane load and recovery measurement.

This harness does not replace service/provider capacity testing. It measures the
queue/backpressure/idempotency design over the repository fixture distribution.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import resource
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass(frozen=True, slots=True)
class Job:
    identity: int
    pages: int
    submitted_at: float
    duplicate: bool = False


@dataclass(slots=True)
class RunMetrics:
    queue_peak: int = 0
    active: int = 0
    active_peak: int = 0
    retries: int = 0
    deleted_during_index: int = 0


async def run_control(
    document_count: int,
    worker_count: int,
    queue_capacity: int,
    fixture_directory: Path,
) -> dict[str, object]:
    page_distribution = _page_distribution(fixture_directory)
    queue: asyncio.Queue[Job | None] = asyncio.Queue(maxsize=queue_capacity)
    metrics = RunMetrics()
    completed: set[int] = set()
    searchable: set[int] = set()
    latencies: list[float] = []
    started = time.perf_counter()

    async def worker() -> None:
        while True:
            job = await queue.get()
            if job is None:
                queue.task_done()
                return
            metrics.active += 1
            metrics.active_peak = max(metrics.active_peak, metrics.active)
            try:
                if job.identity not in completed:
                    if job.identity % 41 == 0:
                        metrics.retries += 1
                        await asyncio.sleep(0)
                    _bounded_page_work(job)
                    completed.add(job.identity)
                    if job.identity % 53 == 0:
                        metrics.deleted_during_index += 1
                    else:
                        searchable.add(job.identity)
                latencies.append(time.perf_counter() - job.submitted_at)
            finally:
                metrics.active -= 1
                queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
    for identity in range(document_count):
        pages = page_distribution[identity % len(page_distribution)]
        await queue.put(Job(identity, pages, time.perf_counter()))
        metrics.queue_peak = max(metrics.queue_peak, queue.qsize())
        if identity % 10 == 0:
            await queue.put(
                Job(identity, pages, time.perf_counter(), duplicate=True)
            )
            metrics.queue_peak = max(metrics.queue_peak, queue.qsize())
    for _ in workers:
        await queue.put(None)
    await queue.join()
    await asyncio.gather(*workers)
    duration = time.perf_counter() - started
    expected_searchable = document_count - sum(
        1 for identity in range(document_count) if identity % 53 == 0
    )
    if len(completed) != document_count or len(searchable) != expected_searchable:
        raise RuntimeError("control run violated idempotent terminal counts")
    return {
        "documents": document_count,
        "pages": sum(
            page_distribution[index % len(page_distribution)]
            for index in range(document_count)
        ),
        "workers": worker_count,
        "queue_capacity": queue_capacity,
        "queue_peak": metrics.queue_peak,
        "active_peak": metrics.active_peak,
        "duration_seconds": round(duration, 6),
        "documents_per_second": round(document_count / duration, 3),
        "latency_p50_ms": round(statistics.median(latencies) * 1000, 3),
        "latency_p95_ms": round(_percentile(latencies, 0.95) * 1000, 3),
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "controlled_retries": metrics.retries,
        "deleted_during_index": metrics.deleted_during_index,
        "duplicate_searchable_records": 0,
        "external_provider_cost_usd": 0,
        "limitations": (
            "local control-plane bound test; excludes network/provider and "
            "production hardware cost"
        ),
    }


def _page_distribution(fixture_directory: Path) -> tuple[int, ...]:
    pages: list[int] = []
    for path in sorted(fixture_directory.glob("*.pdf")):
        try:
            pages.append(len(PdfReader(path, strict=True).pages))
        except Exception:
            continue
    if not pages:
        raise RuntimeError("no valid PDF fixtures are available")
    return tuple(pages)


def _bounded_page_work(job: Job) -> None:
    for page_number in range(job.pages):
        hashlib.sha256(f"{job.identity}:{page_number}".encode()).digest()


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * quantile))
    return ordered[index]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--documents", type=int, default=10_000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--queue-capacity", type=int, default=64)
    parser.add_argument(
        "--fixtures", type=Path, default=Path("benchmarks/fixtures")
    )
    arguments = parser.parse_args()
    if min(arguments.documents, arguments.workers, arguments.queue_capacity) < 1:
        parser.error("documents, workers, and queue capacity must be positive")
    print(
        json.dumps(
            asyncio.run(
                run_control(
                    arguments.documents,
                    arguments.workers,
                    arguments.queue_capacity,
                    arguments.fixtures,
                )
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
