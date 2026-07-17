from app.services.catalog.enums import VersionState


class IngestionError(RuntimeError):
    """Base class for worker-safe ingestion outcomes."""


class JobFenceLostError(IngestionError):
    """Raised when a worker no longer owns its durable job attempt."""


class TerminalIngestionError(IngestionError):
    def __init__(self, state: VersionState, reason_code: str) -> None:
        super().__init__(reason_code)
        self.state = state
        self.reason_code = reason_code


class RetryableIngestionError(IngestionError):
    def __init__(self, reason_code: str, retry_after_seconds: int) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retry_after_seconds = retry_after_seconds
