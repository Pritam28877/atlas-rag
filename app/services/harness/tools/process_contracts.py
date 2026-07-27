"""Strict process tool arguments and bounded model-visible results."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.policy import ExecutableTarget
from app.services.harness.protocol import Sha256, StrictProtocolModel
from app.services.harness.tools.contracts import (
    DEFAULT_TOOL_OUTPUT_BYTES,
    MAXIMUM_TOOL_OUTPUT_BYTES,
    StrictToolArguments,
)

ProcessValue = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=512,
        pattern=r"^[^\x00\r\n]+$",
    ),
]


class RunProcessArguments(StrictToolArguments):
    executable: ProcessValue
    arguments: tuple[ProcessValue, ...] = Field(max_length=64)
    working_directory: ProcessValue

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        ExecutableTarget(
            executable=self.executable,
            arguments=self.arguments,
            working_directory=self.working_directory,
        )
        working_directory = PurePosixPath(self.working_directory)
        if working_directory.as_posix() != self.working_directory:
            raise ValueError("process working directory is not canonical")
        return self


class RunProcessResult(StrictProtocolModel):
    return_code: int = Field(ge=-(2**31), le=2**31 - 1)
    stdout: str = Field(max_length=DEFAULT_TOOL_OUTPUT_BYTES)
    stderr: str = Field(max_length=DEFAULT_TOOL_OUTPUT_BYTES)
    stdout_source_bytes: int = Field(ge=0, le=MAXIMUM_TOOL_OUTPUT_BYTES)
    stderr_source_bytes: int = Field(ge=0, le=MAXIMUM_TOOL_OUTPUT_BYTES)
    stdout_sha256: Sha256
    stderr_sha256: Sha256
    output_bytes: int = Field(ge=0, le=DEFAULT_TOOL_OUTPUT_BYTES)
    truncated: bool
    encoding_replaced: bool

    @model_validator(mode="after")
    def validate_output_size(self) -> Self:
        encoded_bytes = len(self.stdout.encode()) + len(self.stderr.encode())
        if encoded_bytes != self.output_bytes:
            raise ValueError("process output metadata is inconsistent")
        return self
