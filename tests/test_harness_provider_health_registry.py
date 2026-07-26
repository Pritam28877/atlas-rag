import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import (
    ProviderHealthStatus,
    ProviderRouteHealthSnapshot,
    build_provider_health_snapshot,
)
from app.services.harness.providers import (
    ProviderHealthRegistry,
    ProviderHealthRegistryError,
    ProviderHealthRegistryErrorCode,
)

NOW = datetime(2026, 7, 28, 11, 0, tzinfo=UTC)
PRIMARY_ROUTE = "configured.primary"
SECONDARY_ROUTE = "configured.secondary"


def health_snapshot(
    status: ProviderHealthStatus,
    *,
    observed_at: datetime = NOW + timedelta(seconds=1),
    previous_snapshot_sha256: str | None = "0" * 64,
    consecutive_failures: int = 0,
) -> ProviderRouteHealthSnapshot:
    return build_provider_health_snapshot(
        route_id=PRIMARY_ROUTE,
        status=status,
        reason=f"Injected {status.value} observation.",
        observed_at=observed_at,
        valid_until=observed_at + timedelta(seconds=30),
        consecutive_failures=consecutive_failures,
        latency_p95_ms=25,
        previous_snapshot_sha256=previous_snapshot_sha256,
    )


def test_health_snapshot_hash_and_validity_fail_closed() -> None:
    first = health_snapshot(ProviderHealthStatus.HEALTHY)
    second = health_snapshot(ProviderHealthStatus.HEALTHY)

    assert first == second
    tampered = first.model_dump(mode="python")
    tampered["reason"] = "Tampered observation."
    with pytest.raises(ValidationError, match="hash mismatch"):
        ProviderRouteHealthSnapshot.model_validate(tampered)
    with pytest.raises(ValidationError, match="cannot retain failures"):
        health_snapshot(
            ProviderHealthStatus.HEALTHY,
            consecutive_failures=1,
        )
    with pytest.raises(ValidationError, match="within 24 hours"):
        build_provider_health_snapshot(
            route_id=PRIMARY_ROUTE,
            status=ProviderHealthStatus.UNKNOWN,
            reason="Invalid validity.",
            observed_at=NOW,
            valid_until=NOW + timedelta(days=2),
            consecutive_failures=0,
        )


def test_registry_starts_unknown_and_converts_expired_health_to_stale() -> None:
    async def scenario() -> None:
        registry = ProviderHealthRegistry(
            (PRIMARY_ROUTE, SECONDARY_ROUTE),
            observed_at=NOW,
            unknown_ttl_seconds=10,
        )

        unknown = await registry.snapshot(
            PRIMARY_ROUTE,
            observed_at=NOW,
        )
        stale = await registry.snapshot(
            PRIMARY_ROUTE,
            observed_at=NOW + timedelta(seconds=10),
        )
        replay = await registry.snapshot(
            PRIMARY_ROUTE,
            observed_at=NOW + timedelta(seconds=10),
        )

        assert await registry.size() == 2
        assert unknown.status is ProviderHealthStatus.UNKNOWN
        assert stale.status is ProviderHealthStatus.STALE
        assert stale.previous_snapshot_sha256 == unknown.snapshot_sha256
        assert replay == stale

    asyncio.run(scenario())


def test_registry_accepts_only_monotonic_hash_linked_probe_states() -> None:
    async def scenario() -> None:
        registry = ProviderHealthRegistry(
            (PRIMARY_ROUTE,),
            observed_at=NOW,
        )
        initial = await registry.snapshot(
            PRIMARY_ROUTE,
            observed_at=NOW,
        )
        healthy = health_snapshot(
            ProviderHealthStatus.HEALTHY,
            previous_snapshot_sha256=initial.snapshot_sha256,
        )
        await registry.record(healthy)

        wrong_lineage = health_snapshot(
            ProviderHealthStatus.DEGRADED,
            observed_at=NOW + timedelta(seconds=2),
            previous_snapshot_sha256="f" * 64,
            consecutive_failures=1,
        )
        with pytest.raises(ProviderHealthRegistryError) as lineage_error:
            await registry.record(wrong_lineage)
        with pytest.raises(ProviderHealthRegistryError) as stale_error:
            await registry.record(healthy)

        reserved_state = health_snapshot(
            ProviderHealthStatus.UNKNOWN,
            observed_at=NOW + timedelta(seconds=2),
            previous_snapshot_sha256=healthy.snapshot_sha256,
        )
        with pytest.raises(ProviderHealthRegistryError) as state_error:
            await registry.record(reserved_state)
        with pytest.raises(ProviderHealthRegistryError) as route_error:
            await registry.snapshot(
                "configured.unknown",
                observed_at=NOW,
            )

        assert lineage_error.value.code is (
            ProviderHealthRegistryErrorCode.LINEAGE
        )
        assert stale_error.value.code is (
            ProviderHealthRegistryErrorCode.NON_MONOTONIC
        )
        assert state_error.value.code is (
            ProviderHealthRegistryErrorCode.INVALID_STATE
        )
        assert route_error.value.code is (
            ProviderHealthRegistryErrorCode.UNKNOWN_ROUTE
        )

    asyncio.run(scenario())


def test_concurrent_updates_apply_at_most_one_successor() -> None:
    async def scenario() -> None:
        registry = ProviderHealthRegistry(
            (PRIMARY_ROUTE,),
            observed_at=NOW,
        )
        initial = await registry.snapshot(
            PRIMARY_ROUTE,
            observed_at=NOW,
        )
        first = health_snapshot(
            ProviderHealthStatus.DEGRADED,
            observed_at=NOW + timedelta(seconds=1),
            previous_snapshot_sha256=initial.snapshot_sha256,
            consecutive_failures=1,
        )
        second = health_snapshot(
            ProviderHealthStatus.UNAVAILABLE,
            observed_at=NOW + timedelta(seconds=2),
            previous_snapshot_sha256=initial.snapshot_sha256,
            consecutive_failures=2,
        )

        outcomes = await asyncio.gather(
            registry.record(first),
            registry.record(second),
            return_exceptions=True,
        )
        failures = tuple(
            outcome
            for outcome in outcomes
            if isinstance(outcome, ProviderHealthRegistryError)
        )
        current = await registry.snapshot(
            PRIMARY_ROUTE,
            observed_at=NOW + timedelta(seconds=2),
        )

        assert len(failures) == 1
        assert current.snapshot_sha256 in {
            first.snapshot_sha256,
            second.snapshot_sha256,
        }
        assert await registry.size() == 1

    asyncio.run(scenario())


def test_registry_route_set_is_bounded_and_canonical() -> None:
    with pytest.raises(ProviderHealthRegistryError) as duplicate_error:
        ProviderHealthRegistry(
            (PRIMARY_ROUTE, PRIMARY_ROUTE),
            observed_at=NOW,
        )
    with pytest.raises(ProviderHealthRegistryError) as order_error:
        ProviderHealthRegistry(
            (SECONDARY_ROUTE, PRIMARY_ROUTE),
            observed_at=NOW,
        )

    assert duplicate_error.value.code is (
        ProviderHealthRegistryErrorCode.INVALID_ROUTES
    )
    assert order_error.value.code is (
        ProviderHealthRegistryErrorCode.INVALID_ROUTES
    )
