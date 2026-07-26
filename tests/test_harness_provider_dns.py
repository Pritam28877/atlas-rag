import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.services.harness.providers import (
    ProviderDnsError,
    ProviderDnsErrorCode,
    SystemProviderAddressResolver,
)


def deadline() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=1)


def test_resolver_returns_bounded_canonical_unique_addresses() -> None:
    async def lookup(hostname: str, port: int) -> tuple[str, ...]:
        assert (hostname, port) == ("api.provider.example", 443)
        return ("8.8.8.8", "1.1.1.1", "8.8.8.8")

    async def scenario() -> None:
        resolver = SystemProviderAddressResolver(lookup=lookup)
        addresses = await resolver.resolve(
            "api.provider.example",
            443,
            cancellation=asyncio.Event(),
            deadline_at=deadline(),
        )
        assert addresses == ("1.1.1.1", "8.8.8.8")

    asyncio.run(scenario())


def test_resolver_cancellation_joins_blocked_lookup() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def lookup(hostname: str, port: int) -> tuple[str, ...]:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        resolver = SystemProviderAddressResolver(lookup=lookup)
        cancellation = asyncio.Event()
        task = asyncio.create_task(
            resolver.resolve(
                "api.provider.example",
                443,
                cancellation=cancellation,
                deadline_at=deadline(),
            )
        )
        await started.wait()
        cancellation.set()
        with pytest.raises(ProviderDnsError) as captured:
            await task

        assert captured.value.code is ProviderDnsErrorCode.CANCELLED
        assert cleaned.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("addresses", "expected_code"),
    [
        ((), ProviderDnsErrorCode.EMPTY),
        (
            tuple(f"192.0.2.{index}" for index in range(17)),
            ProviderDnsErrorCode.LIMIT,
        ),
    ],
)
def test_resolver_rejects_empty_or_excessive_answers(
    addresses: tuple[str, ...],
    expected_code: ProviderDnsErrorCode,
) -> None:
    async def lookup(hostname: str, port: int) -> tuple[str, ...]:
        return addresses

    async def scenario() -> None:
        resolver = SystemProviderAddressResolver(lookup=lookup)
        with pytest.raises(ProviderDnsError) as captured:
            await resolver.resolve(
                "api.provider.example",
                443,
                cancellation=asyncio.Event(),
                deadline_at=deadline(),
            )
        assert captured.value.code is expected_code

    asyncio.run(scenario())


def test_resolver_sanitizes_lookup_failure_and_expired_deadline() -> None:
    async def failing_lookup(hostname: str, port: int) -> tuple[str, ...]:
        raise RuntimeError("resolver-internal-detail")

    async def scenario() -> None:
        resolver = SystemProviderAddressResolver(lookup=failing_lookup)
        with pytest.raises(ProviderDnsError) as lookup_error:
            await resolver.resolve(
                "api.provider.example",
                443,
                cancellation=asyncio.Event(),
                deadline_at=deadline(),
            )
        with pytest.raises(ProviderDnsError) as deadline_error:
            await resolver.resolve(
                "api.provider.example",
                443,
                cancellation=asyncio.Event(),
                deadline_at=datetime.now(UTC) - timedelta(seconds=1),
            )

        assert lookup_error.value.code is ProviderDnsErrorCode.LOOKUP
        assert "resolver-internal-detail" not in str(lookup_error.value)
        assert deadline_error.value.code is ProviderDnsErrorCode.DEADLINE

    asyncio.run(scenario())
