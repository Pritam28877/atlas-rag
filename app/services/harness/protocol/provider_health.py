"""Hash-linked bounded route-health evidence for provider routing."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedReason,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.routing import RouteId

MAXIMUM_HEALTH_VALIDITY_SECONDS = 24 * 60 * 60


class ProviderHealthStatus(StrEnum):
    DEGRADED = "degraded"
    HEALTHY = "healthy"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ProviderRouteHealthSnapshot(StrictProtocolModel):
    route_id: RouteId
    status: ProviderHealthStatus
    reason: BoundedReason
    observed_at: UtcTimestamp
    valid_until: UtcTimestamp
    consecutive_failures: int = Field(ge=0, le=1_000_000)
    latency_p95_ms: int | None = Field(
        default=None,
        ge=0,
        le=3_600_000,
    )
    previous_snapshot_sha256: Sha256 | None = None
    snapshot_sha256: Sha256

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        validity = self.valid_until - self.observed_at
        if not timedelta(0) < validity <= timedelta(
            seconds=MAXIMUM_HEALTH_VALIDITY_SECONDS
        ):
            raise ValueError("health validity must be within 24 hours")
        if (
            self.status is ProviderHealthStatus.HEALTHY
            and self.consecutive_failures != 0
        ):
            raise ValueError("healthy route cannot retain failures")
        expected_digest = provider_health_snapshot_sha256(
            route_id=self.route_id,
            status=self.status,
            reason=self.reason,
            observed_at=self.observed_at,
            valid_until=self.valid_until,
            consecutive_failures=self.consecutive_failures,
            latency_p95_ms=self.latency_p95_ms,
            previous_snapshot_sha256=self.previous_snapshot_sha256,
        )
        if self.snapshot_sha256 != expected_digest:
            raise ValueError("provider health snapshot hash mismatch")
        return self


def build_provider_health_snapshot(
    *,
    route_id: str,
    status: ProviderHealthStatus,
    reason: str,
    observed_at: datetime,
    valid_until: datetime,
    consecutive_failures: int,
    latency_p95_ms: int | None = None,
    previous_snapshot_sha256: str | None = None,
) -> ProviderRouteHealthSnapshot:
    snapshot_sha256 = provider_health_snapshot_sha256(
        route_id=route_id,
        status=status,
        reason=reason,
        observed_at=observed_at,
        valid_until=valid_until,
        consecutive_failures=consecutive_failures,
        latency_p95_ms=latency_p95_ms,
        previous_snapshot_sha256=previous_snapshot_sha256,
    )
    return ProviderRouteHealthSnapshot(
        route_id=route_id,
        status=status,
        reason=reason,
        observed_at=observed_at,
        valid_until=valid_until,
        consecutive_failures=consecutive_failures,
        latency_p95_ms=latency_p95_ms,
        previous_snapshot_sha256=previous_snapshot_sha256,
        snapshot_sha256=snapshot_sha256,
    )


def provider_health_snapshot_sha256(
    *,
    route_id: str,
    status: ProviderHealthStatus,
    reason: str,
    observed_at: datetime,
    valid_until: datetime,
    consecutive_failures: int,
    latency_p95_ms: int | None,
    previous_snapshot_sha256: str | None,
) -> str:
    payload = {
        "consecutive_failures": consecutive_failures,
        "latency_p95_ms": latency_p95_ms,
        "observed_at": observed_at.isoformat(),
        "previous_snapshot_sha256": previous_snapshot_sha256,
        "reason": reason,
        "route_id": route_id,
        "status": status.value,
        "valid_until": valid_until.isoformat(),
    }
    canonical_json = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical_json.encode()).hexdigest()
