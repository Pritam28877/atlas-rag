"""Canonical built-in registry tests."""

import hashlib
import json

from app.services.harness.protocol import (
    IdempotencyClass,
    ProviderToolCall,
)
from app.services.harness.tools import (
    PATCH_FILE_TOOL_NAME,
    PROCESS_TOOL_NAME,
    READ_FILE_TOOL_NAME,
    SEARCH_FILES_TOOL_NAME,
    builtin_tool_registry,
)


def test_builtin_catalog_has_exact_sorted_security_metadata() -> None:
    registry = builtin_tool_registry()
    descriptors = registry.descriptors

    assert tuple(descriptor.name for descriptor in descriptors) == (
        PROCESS_TOOL_NAME,
        PATCH_FILE_TOOL_NAME,
        READ_FILE_TOOL_NAME,
        SEARCH_FILES_TOOL_NAME,
    )
    assert tuple(descriptor.capability for descriptor in descriptors) == (
        "executable.run",
        "filesystem.write",
        "filesystem.read",
        "filesystem.read",
    )
    assert tuple(descriptor.idempotency_class for descriptor in descriptors) == (
        IdempotencyClass.NON_IDEMPOTENT,
        IdempotencyClass.NON_IDEMPOTENT,
        IdempotencyClass.READ_ONLY,
        IdempotencyClass.READ_ONLY,
    )
    assert all(
        descriptor.output.redaction_required for descriptor in descriptors
    )
    assert all(
        not descriptor.output.background_allowed for descriptor in descriptors
    )


def test_builtin_alias_resolves_to_canonical_validated_call() -> None:
    registry = builtin_tool_registry()
    arguments_json = json.dumps(
        {
            "maximum_bytes": 1024,
            "offset_bytes": 0,
            "path": "src/app.py",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    call = ProviderToolCall(
        sequence=1,
        call_id="call-read",
        tool_name="read_file",
        arguments_json=arguments_json,
        arguments_sha256=hashlib.sha256(
            arguments_json.encode()
        ).hexdigest(),
    )

    validated = registry.validate_provider_call(call)

    assert validated.requested_name == "read_file"
    assert validated.tool_name == READ_FILE_TOOL_NAME
    assert validated.capability == "filesystem.read"
