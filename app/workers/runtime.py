"""Job-scoped worker filesystem and shutdown helpers."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID


@contextmanager
def job_temp_directory(base_directory: Path, job_id: UUID) -> Iterator[Path]:
    """Create a private temporary directory that is removed on every exit path."""
    base_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix=f"job-{job_id}-",
        dir=base_directory,
        ignore_cleanup_errors=True,
    ) as directory:
        yield Path(directory)
