"""Strict bounded contracts for workspace read and literal search tools."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol import Sha256, StrictProtocolModel
from app.services.harness.tools.contracts import StrictToolArguments

MAXIMUM_WORKSPACE_PATH_BYTES = 4096
MAXIMUM_READ_BYTES = 1024 * 1024
MAXIMUM_SEARCH_BYTES = 16 * 1024 * 1024
MAXIMUM_SEARCH_FILES = 4096
MAXIMUM_SEARCH_MATCHES = 1000
MAXIMUM_SEARCH_QUERY_BYTES = 4096
MAXIMUM_MATCH_TEXT_BYTES = 4096

WorkspaceRelativePath = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=MAXIMUM_WORKSPACE_PATH_BYTES,
        pattern=r"^[^\x00]+$",
    ),
]


def validate_workspace_relative_path(
    value: str,
    *,
    allow_root: bool = False,
) -> tuple[str, ...]:
    if allow_root and value == ".":
        return ()
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or value in {".", ".."}
        or any(part in {"", ".", ".."} for part in path.parts)
        or len(value.encode()) > MAXIMUM_WORKSPACE_PATH_BYTES
        or len(path.parts) > 64
    ):
        raise ValueError("workspace path is not canonical and relative")
    return path.parts


class ReadFileArguments(StrictToolArguments):
    path: WorkspaceRelativePath
    offset_bytes: int = Field(default=0, ge=0, le=2**63 - 1)
    maximum_bytes: int = Field(default=64 * 1024, ge=1, le=MAXIMUM_READ_BYTES)

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        validate_workspace_relative_path(self.path)
        return self


class SearchFilesArguments(StrictToolArguments):
    path: WorkspaceRelativePath = "."
    query: str = Field(min_length=1, max_length=4096, pattern=r"^[^\x00]+$")
    case_sensitive: bool = True
    maximum_matches: int = Field(
        default=100,
        ge=1,
        le=MAXIMUM_SEARCH_MATCHES,
    )
    maximum_files: int = Field(
        default=1024,
        ge=1,
        le=MAXIMUM_SEARCH_FILES,
    )
    maximum_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=1,
        le=MAXIMUM_SEARCH_BYTES,
    )

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        validate_workspace_relative_path(self.path, allow_root=True)
        if len(self.query.encode()) > MAXIMUM_SEARCH_QUERY_BYTES:
            raise ValueError("search query exceeds the byte limit")
        return self


class ReadFileResult(StrictProtocolModel):
    path: WorkspaceRelativePath
    offset_bytes: int = Field(ge=0, le=2**63 - 1)
    file_size_bytes: int = Field(ge=0, le=2**63 - 1)
    content: str = Field(max_length=MAXIMUM_READ_BYTES)
    content_bytes: int = Field(ge=0, le=MAXIMUM_READ_BYTES)
    content_sha256: Sha256
    truncated: bool

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        validate_workspace_relative_path(self.path)
        encoded = self.content.encode()
        if (
            len(encoded) != self.content_bytes
            or len(encoded) > MAXIMUM_READ_BYTES
        ):
            raise ValueError("read result content metadata is invalid")
        return self


class SearchMatch(StrictProtocolModel):
    path: WorkspaceRelativePath
    line_number: int = Field(ge=1, le=2**31 - 1)
    line: str = Field(max_length=MAXIMUM_MATCH_TEXT_BYTES)
    line_bytes: int = Field(ge=0, le=MAXIMUM_MATCH_TEXT_BYTES)
    truncated: bool

    @model_validator(mode="after")
    def validate_line(self) -> Self:
        validate_workspace_relative_path(self.path)
        if len(self.line.encode()) != self.line_bytes:
            raise ValueError("search match line metadata is invalid")
        return self


class SearchFilesResult(StrictProtocolModel):
    matches: tuple[SearchMatch, ...] = Field(
        max_length=MAXIMUM_SEARCH_MATCHES
    )
    scanned_files: int = Field(ge=0, le=MAXIMUM_SEARCH_FILES)
    scanned_bytes: int = Field(ge=0, le=MAXIMUM_SEARCH_BYTES)
    skipped_binary_files: int = Field(ge=0, le=MAXIMUM_SEARCH_FILES)
    truncated: bool
