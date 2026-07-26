"""Private policy, SSE, and redacted result I/O for provider smokes."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from app.services.harness.protocol import (
    ProviderName,
    RequestId,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.routing import ModelName
from app.services.harness.providers import OpenRouterProviderPolicy

MAXIMUM_SMOKE_POLICY_BYTES = 64 * 1024
MAXIMUM_SMOKE_SSE_RECORDS = 4_096
MAXIMUM_SMOKE_SSE_RECORD_BYTES = 64 * 1024


class ProviderSmokeResult(StrictProtocolModel):
    provider: ProviderName
    model: ModelName
    request_id: RequestId
    response_status: int = Field(ge=200, le=299)
    response_body_sha256: Sha256
    input_tokens: int = Field(ge=0, le=2_000_000)
    cached_input_tokens: int = Field(ge=0, le=2_000_000)
    output_tokens: int = Field(ge=0, le=512_000)
    reasoning_tokens: int = Field(ge=0, le=512_000)
    charged_cost_microusd: int = Field(ge=1, le=10_000_000_000)
    routing_metadata_sha256: Sha256 | None = None
    output_verified: Literal[True] = True
    audit_events: int = Field(ge=2, le=64)
    active_credential_leases: int = Field(ge=0, le=256)
    completed_at: UtcTimestamp


async def load_openrouter_smoke_policy(
    path: Path,
) -> OpenRouterProviderPolicy:
    content = await asyncio.to_thread(
        _read_private_file,
        path,
        MAXIMUM_SMOKE_POLICY_BYTES,
    )
    try:
        return OpenRouterProviderPolicy.model_validate_json(content)
    except ValidationError:
        raise ValueError("OpenRouter smoke policy is invalid") from None


def sse_data_records(body: bytes) -> tuple[bytes, ...]:
    if not isinstance(body, bytes) or not body:
        raise ValueError("provider smoke response body is empty")
    records: list[bytes] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(b":") or line.startswith(b"event:"):
            continue
        if not line.startswith(b"data:"):
            raise ValueError("provider smoke SSE line is invalid")
        record = line[5:].strip()
        if not 1 <= len(record) <= MAXIMUM_SMOKE_SSE_RECORD_BYTES:
            raise ValueError("provider smoke SSE record size is invalid")
        records.append(record)
        if len(records) > MAXIMUM_SMOKE_SSE_RECORDS:
            raise ValueError("provider smoke SSE record limit exceeded")
    if not records:
        raise ValueError("provider smoke SSE stream has no data")
    return tuple(records)


async def write_provider_smoke_result(
    path: Path,
    result: ProviderSmokeResult,
) -> None:
    content = result.model_dump_json().encode()
    await asyncio.to_thread(_write_private_new_file, path, content)


async def validate_private_smoke_input(path: Path) -> None:
    await asyncio.to_thread(_private_regular_status, path)


async def validate_provider_smoke_output_path(path: Path) -> None:
    await asyncio.to_thread(_validate_new_private_path, path)


def _read_private_file(path: Path, maximum_bytes: int) -> bytes:
    status = _private_regular_status(path)
    if not 1 <= status.st_size <= maximum_bytes:
        raise ValueError("private provider smoke file size is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened_status = os.fstat(descriptor)
        if (
            opened_status.st_dev != status.st_dev
            or opened_status.st_ino != status.st_ino
        ):
            raise ValueError("private provider smoke file changed")
        content = bytearray()
        while True:
            remaining = maximum_bytes + 1 - len(content)
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > maximum_bytes:
                raise ValueError("private provider smoke file is too large")
        return bytes(content)
    finally:
        os.close(descriptor)


def _private_regular_status(path: Path) -> os.stat_result:
    if not path.is_absolute():
        raise ValueError("private provider smoke path must be absolute")
    status = path.lstat()
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.getuid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise ValueError("provider smoke input must be owner-only")
    return status


def _write_private_new_file(path: Path, content: bytes) -> None:
    _validate_new_private_path(path)
    parent = path.parent
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("provider smoke result write failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_descriptor = os.open(
        parent,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _validate_new_private_path(path: Path) -> None:
    parent = path.parent
    if parent.resolve(strict=True) != parent:
        raise ValueError("provider smoke result directory is not canonical")
    parent_status = parent.stat()
    if (
        not stat.S_ISDIR(parent_status.st_mode)
        or parent_status.st_uid != os.getuid()
        or stat.S_IMODE(parent_status.st_mode) & 0o077
    ):
        raise ValueError("provider smoke result directory must be owner-only")
    if os.path.lexists(path):
        raise ValueError("provider smoke result already exists")


def response_body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
