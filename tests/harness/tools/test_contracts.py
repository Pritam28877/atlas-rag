"""Stable immutable tool contract tests."""

import hashlib

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import IdempotencyClass
from app.services.harness.tools import (
    ToolDescriptor,
    ToolOutputContract,
    ToolOutputOverflow,
    build_tool_descriptor,
    canonical_tool_schema,
)
from tests.harness.tools.fixtures import ReadFileArguments


def test_descriptor_binds_schema_policy_and_output() -> None:
    descriptor = build_tool_descriptor(
        name="workspace.read_file",
        version="1.0.0",
        aliases=("read_file",),
        default_version=True,
        capability="filesystem.read",
        idempotency_class=IdempotencyClass.READ_ONLY,
        argument_model=ReadFileArguments,
        output=ToolOutputContract(
            maximum_bytes=1024 * 1024,
            maximum_inline_bytes=64 * 1024,
            overflow=ToolOutputOverflow.ARTIFACT,
        ),
    )

    assert descriptor.input_schema_sha256 == hashlib.sha256(
        canonical_tool_schema(ReadFileArguments).encode()
    ).hexdigest()
    assert descriptor.output.redaction_required
    with pytest.raises(ValidationError, match="descriptor hash"):
        ToolDescriptor.model_validate(
            {
                **descriptor.model_dump(),
                "capability": "filesystem.write",
            }
        )


def test_aliases_and_inline_output_are_strictly_bounded() -> None:
    descriptor = build_tool_descriptor(
        name="workspace.read_file",
        version="1.0.0",
        aliases=("read_file",),
        default_version=True,
        capability="filesystem.read",
        idempotency_class=IdempotencyClass.READ_ONLY,
        argument_model=ReadFileArguments,
        output=ToolOutputContract(
            maximum_bytes=1024,
            maximum_inline_bytes=1024,
            overflow=ToolOutputOverflow.TRUNCATE,
        ),
    )

    with pytest.raises(ValidationError, match="aliases"):
        ToolDescriptor.model_validate(
            {
                **descriptor.model_dump(),
                "aliases": ("read_file", "read_file"),
            }
        )
    with pytest.raises(ValidationError, match="inline output"):
        ToolOutputContract(
            maximum_bytes=1024,
            maximum_inline_bytes=2048,
            overflow=ToolOutputOverflow.REJECT,
        )
