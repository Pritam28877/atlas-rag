import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.services.harness.providers import (
    BedrockCredentialMaterial,
    BedrockCredentialResolutionError,
    BedrockCredentialResolutionErrorCode,
    BoundedBedrockCredentialResolver,
)
from app.services.harness.providers.bedrock_identity import (
    BedrockCredentialSourceKind,
    BedrockIdentityReference,
)


class Backend:
    def __init__(self) -> None:
        self.identities: list[BedrockIdentityReference] = []

    def load(
        self,
        identity: BedrockIdentityReference,
    ) -> BedrockCredentialMaterial:
        self.identities.append(identity)
        return BedrockCredentialMaterial(
            bytearray(b"access-canary"),
            bytearray(b"secret-canary"),
            bytearray(b"token-canary"),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


def test_bounded_resolver_loads_only_explicit_identity_and_zeros_material() -> None:
    async def scenario() -> None:
        backend = Backend()
        resolver = BoundedBedrockCredentialResolver(
            backend,
            clock=lambda: datetime.now(UTC),
            maximum_workers=1,
            maximum_pending=1,
        )
        identity = _identity()

        material = await resolver.resolve(
            identity,
            cancellation=asyncio.Event(),
            deadline_at=datetime.now(UTC) + timedelta(seconds=5),
        )

        assert backend.identities == [identity]
        access_key, secret_key, token = material.views()
        assert bytes(access_key) == b"access-canary"
        assert bytes(secret_key) == b"secret-canary"
        assert token is not None and bytes(token) == b"token-canary"
        assert "canary" not in repr(material)
        material.zero()
        assert bytes(access_key) == bytes(len(access_key))
        assert bytes(secret_key) == bytes(len(secret_key))
        await resolver.close()

    asyncio.run(scenario())


def test_pre_cancelled_or_expired_resolution_never_calls_backend() -> None:
    async def scenario() -> None:
        backend = Backend()
        resolver = BoundedBedrockCredentialResolver(
            backend,
            clock=lambda: datetime.now(UTC),
        )
        cancellation = asyncio.Event()
        cancellation.set()
        with pytest.raises(BedrockCredentialResolutionError) as cancelled:
            await resolver.resolve(
                _identity(),
                cancellation=cancellation,
                deadline_at=datetime.now(UTC) + timedelta(seconds=5),
            )
        with pytest.raises(BedrockCredentialResolutionError) as expired:
            await resolver.resolve(
                _identity(),
                cancellation=asyncio.Event(),
                deadline_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        assert cancelled.value.code is (BedrockCredentialResolutionErrorCode.CANCELLED)
        assert expired.value.code is BedrockCredentialResolutionErrorCode.DEADLINE
        assert backend.identities == []
        await resolver.close()

    asyncio.run(scenario())


def test_invalid_material_is_zeroed_before_rejection() -> None:
    access_key = bytearray()
    secret_key = bytearray(b"must-be-zeroed")

    with pytest.raises(BedrockCredentialResolutionError) as captured:
        BedrockCredentialMaterial(
            access_key,
            secret_key,
            None,
            expires_at=None,
        )

    assert captured.value.code is BedrockCredentialResolutionErrorCode.INVALID
    assert secret_key == bytearray(len(secret_key))


def _identity() -> BedrockIdentityReference:
    return BedrockIdentityReference(
        identity_reference_id="awsid_" + "1" * 32,
        credential_handle="pcr_" + "2" * 32,
        source=BedrockCredentialSourceKind.INSTANCE_METADATA,
    )
