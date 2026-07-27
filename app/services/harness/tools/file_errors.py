"""Stable redacted errors for workspace file operations."""

from enum import StrEnum


class WorkspaceFileErrorCode(StrEnum):
    AMBIGUOUS = "ambiguous"
    CAPACITY = "capacity"
    CANCELLED = "cancelled"
    CLOSED = "closed"
    CONFLICT = "conflict"
    INVALID_PATH = "invalid_path"
    INVALID_TEXT = "invalid_text"
    IO = "io"
    LIMIT = "limit"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    UNSUPPORTED = "unsupported"


class WorkspaceFileError(RuntimeError):
    def __init__(self, code: WorkspaceFileErrorCode) -> None:
        super().__init__("workspace file operation failed")
        self.code = code
