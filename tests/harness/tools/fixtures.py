"""Canonical tool registry fixtures."""

import hashlib
import json

from pydantic import Field

from app.services.harness.protocol import IdempotencyClass, ProviderToolCall
from app.services.harness.tools import (
    StrictToolArguments,
    ToolOutputContract,
    ToolOutputOverflow,
    ToolRegistration,
    build_tool_descriptor,
)


class ReadFileArguments(StrictToolArguments):
    path: str = Field(min_length=1, max_length=4096, pattern=r"^[^\x00]+$")
    offset: int = Field(default=0, ge=0, le=2**63 - 1)
    limit: int = Field(default=4096, ge=1, le=1024 * 1024)


class SearchArguments(StrictToolArguments):
    query: str = Field(min_length=1, max_length=4096, pattern=r"^[^\x00]+$")
    maximum_matches: int = Field(default=100, ge=1, le=1000)


def registration(
    version: str,
    *,
    default_version: bool,
    name: str = "workspace.read_file",
    aliases: tuple[str, ...] = ("read_file",),
    argument_model: type[StrictToolArguments] = ReadFileArguments,
) -> ToolRegistration:
    descriptor = build_tool_descriptor(
        name=name,
        version=version,
        aliases=aliases,
        default_version=default_version,
        capability="filesystem.read",
        idempotency_class=IdempotencyClass.READ_ONLY,
        argument_model=argument_model,
        output=ToolOutputContract(
            maximum_bytes=1024 * 1024,
            maximum_inline_bytes=64 * 1024,
            overflow=ToolOutputOverflow.ARTIFACT,
        ),
    )
    return ToolRegistration(
        descriptor=descriptor,
        argument_model=argument_model,
    )


def provider_call(
    *,
    name: str = "workspace.read_file",
    arguments: object | None = None,
) -> ProviderToolCall:
    values = {"path": "src/app.py"} if arguments is None else arguments
    arguments_json = json.dumps(
        values,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ProviderToolCall(
        sequence=1,
        call_id="call-1",
        tool_name=name,
        arguments_json=arguments_json,
        arguments_sha256=hashlib.sha256(arguments_json.encode()).hexdigest(),
    )
