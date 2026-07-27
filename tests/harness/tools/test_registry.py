"""Fail-closed immutable tool registry tests."""

import hashlib

import pytest

from app.services.harness.protocol.operation_admission import (
    canonical_operation_args_sha256,
)
from app.services.harness.protocol.provider_stream import ProviderToolCall
from app.services.harness.tools import (
    ToolRegistration,
    ToolRegistrationError,
    ToolRegistry,
    ToolRegistryError,
    ToolRegistryErrorCode,
)
from tests.harness.tools.fixtures import (
    SearchArguments,
    provider_call,
    registration,
)


def registry() -> ToolRegistry:
    return ToolRegistry(
        (
            registration("1.0.0", default_version=False),
            registration("2.0.0", default_version=True),
        )
    )


def test_alias_and_version_resolve_to_stable_operation_metadata() -> None:
    tools = registry()
    resolved = tools.validate_provider_call(
        provider_call(name="read_file"),
    )
    previous = tools.validate_provider_call(
        provider_call(name="read_file"),
        version="1.0.0",
    )

    assert resolved.requested_name == "read_file"
    assert resolved.tool_name == "workspace.read_file"
    assert resolved.tool_version == "2.0.0"
    assert previous.tool_version == "1.0.0"
    assert resolved.arguments_json == (
        '{"limit":4096,"offset":0,"path":"src/app.py"}'
    )
    assert resolved.args_sha256 == canonical_operation_args_sha256(
        {"limit": 4096, "offset": 0, "path": "src/app.py"}
    )
    assert resolved.capability == "filesystem.read"


def test_unknown_tool_version_and_invalid_arguments_fail_closed() -> None:
    tools = registry()
    with pytest.raises(ToolRegistryError) as unknown:
        tools.validate_provider_call(provider_call(name="missing"))
    assert unknown.value.code is ToolRegistryErrorCode.UNKNOWN_TOOL

    with pytest.raises(ToolRegistryError) as version:
        tools.validate_provider_call(
            provider_call(),
            version="9.0.0",
        )
    assert version.value.code is ToolRegistryErrorCode.UNKNOWN_TOOL

    for arguments in (
        {"path": 3},
        {"path": "src/app.py", "unknown": True},
        {"path": "bad\u0000path"},
        {"path": "src/app.py", "ratio": 1.5},
    ):
        with pytest.raises(ToolRegistryError) as invalid:
            tools.validate_provider_call(
                provider_call(arguments=arguments)
            )
        assert invalid.value.code is ToolRegistryErrorCode.INVALID_ARGUMENTS


def test_oversized_arguments_fail_before_model_validation() -> None:
    call = provider_call(arguments={"path": "a" * (65 * 1024)})

    with pytest.raises(ToolRegistryError) as failure:
        registry().validate_provider_call(call)

    assert failure.value.code is ToolRegistryErrorCode.ARGUMENT_LIMIT


def test_validated_call_is_rebound_to_immutable_registration() -> None:
    tools = registry()
    resolved = tools.validate_provider_call(
        provider_call(name="read_file"),
    )

    assert tools.revalidate_call(resolved) == resolved

    forged = resolved.model_copy(update={"descriptor_sha256": "f" * 64})
    with pytest.raises(ToolRegistryError) as failure:
        tools.revalidate_call(forged)
    assert failure.value.code is ToolRegistryErrorCode.INVALID_ARGUMENTS


@pytest.mark.parametrize(
    "arguments_json",
    (
        "",
        "null",
        "[]",
        '{"path":"src/app.py","path":"other.py"}',
        '{"path":NaN}',
        '{"path":"src/app.py"} trailing',
        '{"path":' + "[" * 128 + "0" + "]" * 128 + "}",
    ),
)
def test_malformed_corpus_never_escapes_as_an_internal_error(
    arguments_json: str,
) -> None:
    call = ProviderToolCall.model_construct(
        sequence=1,
        call_id="call-1",
        tool_name="workspace.read_file",
        arguments_json=arguments_json,
        arguments_sha256=hashlib.sha256(arguments_json.encode()).hexdigest(),
    )

    with pytest.raises(ToolRegistryError) as failure:
        registry().validate_provider_call(call)

    assert failure.value.code is ToolRegistryErrorCode.INVALID_ARGUMENTS


def test_registry_rejects_schema_mismatch_and_ambiguous_definitions() -> None:
    read_registration = registration("1.0.0", default_version=True)
    search_registration = registration(
        "1.0.0",
        default_version=True,
        name="workspace.search",
        aliases=("search",),
        argument_model=SearchArguments,
    )
    with pytest.raises(ToolRegistrationError, match="schema"):
        ToolRegistration(
            descriptor=read_registration.descriptor,
            argument_model=SearchArguments,
        )
    forged_descriptor = read_registration.descriptor.model_copy(
        update={"capability": "filesystem.write"}
    )
    with pytest.raises(ToolRegistrationError, match="contract"):
        ToolRegistration(
            descriptor=forged_descriptor,
            argument_model=read_registration.argument_model,
        )
    with pytest.raises(ToolRegistrationError, match="sorted"):
        ToolRegistry((search_registration, read_registration))
    with pytest.raises(ToolRegistrationError, match="default"):
        ToolRegistry(
            (
                registration("1.0.0", default_version=True),
                registration("2.0.0", default_version=True),
            )
        )


def test_registry_snapshot_cannot_be_mutated() -> None:
    tools = registry()
    descriptors = tools.descriptors

    assert isinstance(descriptors, tuple)
    with pytest.raises(TypeError):
        descriptors[0].aliases[0] = "changed"  # type: ignore[index]
