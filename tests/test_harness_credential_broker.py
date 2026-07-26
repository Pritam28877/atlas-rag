import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.services.harness.protocol import CredentialSource
from app.services.harness.providers import (
    ConfiguredCredentialBroker,
    CredentialBrokerError,
    CredentialBrokerErrorCode,
    CredentialLease,
    CredentialSecretMaterial,
)
from tests.harness_configured_catalog_fixtures import (
    configured_catalog_fixture,
)

NOW = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
HANDLE = "pcr_" + "9" * 32
PROVIDER = "configured-provider"
DESTINATION_SHA256 = "6" * 64


class RecordedSecretBackend:
    def __init__(
        self,
        *,
        expires_at: datetime = NOW + timedelta(minutes=5),
        blocked: bool = False,
        failure: Exception | None = None,
    ) -> None:
        self.expires_at = expires_at
        self.failure = failure
        self.load_count = 0
        self.buffers: list[bytearray] = []
        self.started = asyncio.Event()
        self.unblock = asyncio.Event()
        if not blocked:
            self.unblock.set()

    async def load(self, handle: str) -> CredentialSecretMaterial:
        self.load_count += 1
        self.started.set()
        await self.unblock.wait()
        if self.failure is not None:
            raise self.failure
        secret = bytearray(f"secret-{self.load_count}".encode())
        self.buffers.append(secret)
        return CredentialSecretMaterial(
            secret,
            expires_at=self.expires_at,
        )


def broker(
    backend: RecordedSecretBackend,
    *,
    maximum_active_leases: int = 256,
) -> ConfiguredCredentialBroker:
    return ConfiguredCredentialBroker(
        configured_catalog_fixture(),
        backend,
        clock=lambda: NOW,
        maximum_active_leases=maximum_active_leases,
    )


async def acquire(
    configured_broker: ConfiguredCredentialBroker,
    *,
    cancellation: asyncio.Event | None = None,
    deadline_at: datetime = NOW + timedelta(seconds=1),
) -> CredentialLease:
    source: CredentialSource[CredentialLease] = configured_broker
    return await source.acquire(
        HANDLE,
        provider=PROVIDER,
        destination_sha256=DESTINATION_SHA256,
        cancellation=cancellation or asyncio.Event(),
        deadline_at=deadline_at,
    )


def test_lease_is_scoped_redacted_and_zeroed_on_release() -> None:
    async def scenario() -> None:
        backend = RecordedSecretBackend()
        configured_broker = broker(backend)

        lease = await acquire(configured_broker)
        secret_view = lease.secret_view()

        assert bytes(secret_view) == b"secret-1"
        assert secret_view.readonly
        assert "secret-1" not in repr(lease)
        assert "<redacted>" in repr(lease)
        assert await configured_broker.active_leases() == 1

        await configured_broker.release(lease)
        await configured_broker.release(lease)

        assert bytes(secret_view) == bytes(len(secret_view))
        assert backend.buffers[0] == bytearray(len(backend.buffers[0]))
        assert await configured_broker.active_leases() == 0
        with pytest.raises(CredentialBrokerError) as released:
            lease.secret_view()
        assert released.value.code is CredentialBrokerErrorCode.RELEASED

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("handle", "provider", "destination", "expected_code"),
    [
        (
            "pcr_" + "a" * 32,
            PROVIDER,
            DESTINATION_SHA256,
            CredentialBrokerErrorCode.PROVIDER_MISMATCH,
        ),
        (
            HANDLE,
            "secondary-provider",
            DESTINATION_SHA256,
            CredentialBrokerErrorCode.PROVIDER_MISMATCH,
        ),
        (
            HANDLE,
            PROVIDER,
            "f" * 64,
            CredentialBrokerErrorCode.DESTINATION_MISMATCH,
        ),
        (
            "pcr_" + "b" * 32,
            PROVIDER,
            DESTINATION_SHA256,
            CredentialBrokerErrorCode.UNKNOWN_HANDLE,
        ),
    ],
)
def test_binding_misuse_fails_before_backend_resolution(
    handle: str,
    provider: str,
    destination: str,
    expected_code: CredentialBrokerErrorCode,
) -> None:
    async def scenario() -> None:
        backend = RecordedSecretBackend()
        configured_broker = broker(backend)

        with pytest.raises(CredentialBrokerError) as captured:
            await configured_broker.acquire(
                handle,
                provider=provider,
                destination_sha256=destination,
                cancellation=asyncio.Event(),
                deadline_at=NOW + timedelta(seconds=1),
            )

        assert captured.value.code is expected_code
        assert backend.load_count == 0

    asyncio.run(scenario())


def test_expired_and_capacity_rejected_material_is_zeroed() -> None:
    async def expired_scenario() -> None:
        backend = RecordedSecretBackend(expires_at=NOW)
        with pytest.raises(CredentialBrokerError) as captured:
            await acquire(broker(backend))
        assert captured.value.code is CredentialBrokerErrorCode.EXPIRED
        assert backend.buffers[0] == bytearray(len(backend.buffers[0]))

    async def capacity_scenario() -> None:
        backend = RecordedSecretBackend()
        configured_broker = broker(backend, maximum_active_leases=1)
        first = await acquire(configured_broker)
        with pytest.raises(CredentialBrokerError) as captured:
            await acquire(configured_broker)
        assert captured.value.code is CredentialBrokerErrorCode.ACTIVE_LIMIT
        assert backend.buffers[1] == bytearray(len(backend.buffers[1]))
        await configured_broker.release(first)

    asyncio.run(expired_scenario())
    asyncio.run(capacity_scenario())


def test_refresh_replaces_a_full_lease_slot_and_zeros_prior_material() -> None:
    async def scenario() -> None:
        backend = RecordedSecretBackend()
        configured_broker = broker(backend, maximum_active_leases=1)
        first = await acquire(configured_broker)
        first_view = first.secret_view()

        refreshed = await configured_broker.refresh(
            first,
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=1),
        )

        assert bytes(first_view) == bytes(len(first_view))
        assert first.released
        assert bytes(refreshed.secret_view()) == b"secret-2"
        assert await configured_broker.active_leases() == 1
        await configured_broker.release(refreshed)

    asyncio.run(scenario())


def test_cancellation_and_deadline_stop_blocked_resolution() -> None:
    async def cancelled_scenario() -> None:
        backend = RecordedSecretBackend(blocked=True)
        configured_broker = broker(backend)
        cancellation = asyncio.Event()
        task = asyncio.create_task(
            acquire(configured_broker, cancellation=cancellation)
        )
        await backend.started.wait()
        cancellation.set()

        with pytest.raises(CredentialBrokerError) as captured:
            await task
        assert captured.value.code is CredentialBrokerErrorCode.CANCELLED
        assert await configured_broker.active_leases() == 0

    async def deadline_scenario() -> None:
        backend = RecordedSecretBackend(blocked=True)
        configured_broker = broker(backend)
        with pytest.raises(CredentialBrokerError) as captured:
            await acquire(
                configured_broker,
                deadline_at=NOW + timedelta(milliseconds=10),
            )
        assert captured.value.code is CredentialBrokerErrorCode.DEADLINE
        assert await configured_broker.active_leases() == 0

    asyncio.run(cancelled_scenario())
    asyncio.run(deadline_scenario())


def test_backend_errors_and_foreign_release_are_sanitized() -> None:
    async def scenario() -> None:
        failing_backend = RecordedSecretBackend(
            failure=RuntimeError("secret-backend-detail")
        )
        with pytest.raises(CredentialBrokerError) as backend_error:
            await acquire(broker(failing_backend))
        assert backend_error.value.code is CredentialBrokerErrorCode.BACKEND
        assert "secret-backend-detail" not in str(backend_error.value)
        assert backend_error.value.__cause__ is None
        assert backend_error.value.__suppress_context__

        first_broker = broker(RecordedSecretBackend())
        second_broker = broker(RecordedSecretBackend())
        lease = await acquire(first_broker)
        with pytest.raises(CredentialBrokerError) as foreign_error:
            await second_broker.release(lease)
        assert foreign_error.value.code is CredentialBrokerErrorCode.UNKNOWN_HANDLE
        assert not lease.released
        await first_broker.release(lease)

    asyncio.run(scenario())
