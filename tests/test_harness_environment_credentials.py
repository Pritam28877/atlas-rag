import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services.harness.providers import (
    EnvironmentCredentialBackend,
    EnvironmentCredentialError,
    EnvironmentCredentialErrorCode,
    EnvironmentCredentialReference,
)
from app.services.harness.providers.credential_material import (
    MAXIMUM_CREDENTIAL_BYTES,
)

NOW = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)
HANDLE = "pcr_" + "9" * 32


def reference(
    *,
    handle: str = HANDLE,
    environment_variable: str = "ATLAS_TEST_PROVIDER_KEY",
) -> EnvironmentCredentialReference:
    return EnvironmentCredentialReference(
        handle=handle,
        environment_variable=environment_variable,
        lease_ttl_seconds=60,
    )


def test_backend_reads_only_explicit_byte_environment_reference() -> None:
    async def scenario() -> None:
        environment = {
            b"ATLAS_TEST_PROVIDER_KEY": b"temporary-secret",
            b"UNRELATED_SECRET": b"must-not-be-read",
        }
        backend = EnvironmentCredentialBackend(
            (reference(),),
            development_mode=True,
            clock=lambda: NOW,
            environment=environment,
        )

        material = await backend.load(HANDLE)
        secret = material.claim()

        assert secret == bytearray(b"temporary-secret")
        assert material.expires_at == NOW + timedelta(seconds=60)
        assert "temporary-secret" not in repr(material)
        secret[:] = bytes(len(secret))

    asyncio.run(scenario())


def test_backend_is_explicitly_disabled_outside_development() -> None:
    with pytest.raises(EnvironmentCredentialError) as captured:
        EnvironmentCredentialBackend(
            (reference(),),
            development_mode=False,
            clock=lambda: NOW,
            environment={},
        )

    assert captured.value.code is EnvironmentCredentialErrorCode.DISABLED


def test_missing_and_oversized_values_fail_without_secret_details() -> None:
    async def missing_scenario() -> None:
        backend = EnvironmentCredentialBackend(
            (reference(),),
            development_mode=True,
            clock=lambda: NOW,
            environment={},
        )
        with pytest.raises(EnvironmentCredentialError) as captured:
            await backend.load(HANDLE)
        assert captured.value.code is EnvironmentCredentialErrorCode.MISSING
        assert "ATLAS_TEST_PROVIDER_KEY" not in str(captured.value)

    async def oversized_scenario() -> None:
        backend = EnvironmentCredentialBackend(
            (reference(),),
            development_mode=True,
            clock=lambda: NOW,
            environment={
                b"ATLAS_TEST_PROVIDER_KEY": (
                    b"x" * (MAXIMUM_CREDENTIAL_BYTES + 1)
                )
            },
        )
        with pytest.raises(EnvironmentCredentialError) as captured:
            await backend.load(HANDLE)
        assert captured.value.code is EnvironmentCredentialErrorCode.SIZE

    asyncio.run(missing_scenario())
    asyncio.run(oversized_scenario())


def test_reference_allowlist_is_bounded_canonical_and_strict() -> None:
    second = reference(
        handle="pcr_" + "a" * 32,
        environment_variable="ATLAS_SECOND_PROVIDER_KEY",
    )
    with pytest.raises(EnvironmentCredentialError) as duplicate_handle:
        EnvironmentCredentialBackend(
            (reference(), reference()),
            development_mode=True,
            clock=lambda: NOW,
            environment={},
        )
    with pytest.raises(EnvironmentCredentialError) as unordered:
        EnvironmentCredentialBackend(
            (second, reference()),
            development_mode=True,
            clock=lambda: NOW,
            environment={},
        )
    with pytest.raises(EnvironmentCredentialError) as duplicate_variable:
        EnvironmentCredentialBackend(
            (
                reference(),
                reference(
                    handle="pcr_" + "a" * 32,
                    environment_variable="ATLAS_TEST_PROVIDER_KEY",
                ),
            ),
            development_mode=True,
            clock=lambda: NOW,
            environment={},
        )
    with pytest.raises(ValidationError):
        reference(environment_variable="lowercase-secret")

    assert duplicate_handle.value.code is (
        EnvironmentCredentialErrorCode.INVALID_REFERENCES
    )
    assert unordered.value.code is (
        EnvironmentCredentialErrorCode.INVALID_REFERENCES
    )
    assert duplicate_variable.value.code is (
        EnvironmentCredentialErrorCode.INVALID_REFERENCES
    )
