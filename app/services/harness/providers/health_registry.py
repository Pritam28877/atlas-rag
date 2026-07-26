"""Bounded latest-only provider route-health registry."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from enum import StrEnum
from typing import Never

from app.services.harness.protocol import (
    ProviderHealthStatus,
    ProviderRouteHealthSnapshot,
    RouteId,
    UtcTimestamp,
    build_provider_health_snapshot,
)

MAXIMUM_HEALTH_ROUTES = 1_024


class ProviderHealthRegistryErrorCode(StrEnum):
    INVALID_ROUTES = "invalid_routes"
    INVALID_STATE = "invalid_state"
    LINEAGE = "lineage"
    NON_MONOTONIC = "non_monotonic"
    UNKNOWN_ROUTE = "unknown_route"


class ProviderHealthRegistryError(RuntimeError):
    def __init__(self, code: ProviderHealthRegistryErrorCode) -> None:
        super().__init__("provider health registry operation rejected")
        self.code = code


class ProviderHealthRegistry:
    """Keeps exactly one hash-linked health snapshot per configured route."""

    def __init__(
        self,
        route_ids: tuple[RouteId, ...],
        *,
        observed_at: UtcTimestamp,
        unknown_ttl_seconds: int = 60,
    ) -> None:
        if not 1 <= len(route_ids) <= MAXIMUM_HEALTH_ROUTES:
            self._reject(ProviderHealthRegistryErrorCode.INVALID_ROUTES)
        if tuple(sorted(set(route_ids))) != route_ids:
            self._reject(ProviderHealthRegistryErrorCode.INVALID_ROUTES)
        if not 1 <= unknown_ttl_seconds <= 3_600:
            raise ValueError("unknown health TTL must be between 1 and 3600")
        valid_until = observed_at + timedelta(
            seconds=unknown_ttl_seconds
        )
        self._unknown_ttl_seconds = unknown_ttl_seconds
        self._snapshots = {
            route_id: build_provider_health_snapshot(
                route_id=route_id,
                status=ProviderHealthStatus.UNKNOWN,
                reason="No provider health observation is available.",
                observed_at=observed_at,
                valid_until=valid_until,
                consecutive_failures=0,
            )
            for route_id in route_ids
        }
        self._lock = asyncio.Lock()

    async def size(self) -> int:
        async with self._lock:
            return len(self._snapshots)

    async def record(
        self,
        snapshot: ProviderRouteHealthSnapshot,
    ) -> None:
        async with self._lock:
            current = self._snapshots.get(snapshot.route_id)
            if current is None:
                self._reject(ProviderHealthRegistryErrorCode.UNKNOWN_ROUTE)
            if snapshot.status in {
                ProviderHealthStatus.STALE,
                ProviderHealthStatus.UNKNOWN,
            }:
                self._reject(ProviderHealthRegistryErrorCode.INVALID_STATE)
            if snapshot.observed_at <= current.observed_at:
                self._reject(ProviderHealthRegistryErrorCode.NON_MONOTONIC)
            if (
                snapshot.previous_snapshot_sha256
                != current.snapshot_sha256
            ):
                self._reject(ProviderHealthRegistryErrorCode.LINEAGE)
            self._snapshots[snapshot.route_id] = snapshot

    async def snapshot(
        self,
        route_id: RouteId,
        *,
        observed_at: UtcTimestamp,
    ) -> ProviderRouteHealthSnapshot:
        async with self._lock:
            current = self._snapshots.get(route_id)
            if current is None:
                self._reject(ProviderHealthRegistryErrorCode.UNKNOWN_ROUTE)
            if observed_at < current.observed_at:
                self._reject(ProviderHealthRegistryErrorCode.NON_MONOTONIC)
            if observed_at < current.valid_until:
                return current
            stale = build_provider_health_snapshot(
                route_id=route_id,
                status=ProviderHealthStatus.STALE,
                reason="Latest provider health observation expired.",
                observed_at=observed_at,
                valid_until=observed_at
                + timedelta(seconds=self._unknown_ttl_seconds),
                consecutive_failures=current.consecutive_failures,
                latency_p95_ms=current.latency_p95_ms,
                previous_snapshot_sha256=current.snapshot_sha256,
            )
            self._snapshots[route_id] = stale
            return stale

    @staticmethod
    def _reject(code: ProviderHealthRegistryErrorCode) -> Never:
        raise ProviderHealthRegistryError(code)
