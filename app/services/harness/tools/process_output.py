"""Bounded process output normalization for model-visible results."""

import hashlib

from app.services.harness.sandbox import SandboxProcessResult
from app.services.harness.tools.process_contracts import RunProcessResult


def normalize_process_result(
    result: SandboxProcessResult,
    *,
    maximum_inline_bytes: int,
) -> RunProcessResult:
    stdout_source = result.stdout
    stderr_source = result.stderr
    stdout_prefix = stdout_source[:maximum_inline_bytes]
    remaining_bytes = maximum_inline_bytes - len(stdout_prefix)
    stderr_prefix = stderr_source[:remaining_bytes]
    stdout, stdout_replaced = _safe_text(stdout_prefix)
    stderr, stderr_replaced = _safe_text(stderr_prefix)
    stdout, stdout_truncated = _truncate_utf8(stdout, maximum_inline_bytes)
    stderr_limit = maximum_inline_bytes - len(stdout.encode())
    stderr, stderr_truncated = _truncate_utf8(stderr, stderr_limit)
    output_bytes = len(stdout.encode()) + len(stderr.encode())
    return RunProcessResult(
        return_code=result.return_code,
        stdout=stdout,
        stderr=stderr,
        stdout_source_bytes=len(stdout_source),
        stderr_source_bytes=len(stderr_source),
        stdout_sha256=hashlib.sha256(stdout_source).hexdigest(),
        stderr_sha256=hashlib.sha256(stderr_source).hexdigest(),
        output_bytes=output_bytes,
        truncated=(
            len(stdout_prefix) != len(stdout_source)
            or len(stderr_prefix) != len(stderr_source)
            or stdout_truncated
            or stderr_truncated
        ),
        encoding_replaced=stdout_replaced or stderr_replaced,
    )


def _safe_text(value: bytes) -> tuple[str, bool]:
    try:
        decoded = value.decode("utf-8")
        encoding_replaced = False
    except UnicodeDecodeError:
        decoded = value.decode("utf-8", errors="replace")
        encoding_replaced = True
    characters: list[str] = []
    control_replaced = False
    for character in decoded:
        codepoint = ord(character)
        if codepoint < 32 and character not in {"\t", "\n"}:
            characters.append("\ufffd")
            control_replaced = True
        elif codepoint == 127:
            characters.append("\ufffd")
            control_replaced = True
        else:
            characters.append(character)
    return "".join(characters), encoding_replaced or control_replaced


def _truncate_utf8(value: str, maximum_bytes: int) -> tuple[str, bool]:
    encoded = value.encode()
    if len(encoded) <= maximum_bytes:
        return value, False
    shortened = encoded[:maximum_bytes]
    while shortened:
        try:
            return shortened.decode("utf-8"), True
        except UnicodeDecodeError as error:
            shortened = shortened[: error.start]
    return "", True
