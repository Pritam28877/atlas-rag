"""Exercise real configuration, health, inventory, and deterministic routing."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services.harness.protocol import (
    DataClassification,
    ProviderHealthStatus,
    ProviderRequirements,
    build_provider_health_snapshot,
)
from app.services.harness.providers import (
    ConfiguredRouteInventory,
    ProviderHealthRegistry,
    load_provider_configuration,
)
from app.services.harness.runtime import select_provider_route

NOW = datetime(2026, 7, 28, 14, 0, tzinfo=UTC)


async def _record_health(
    registry: ProviderHealthRegistry,
    route_id: str,
    status: ProviderHealthStatus,
) -> None:
    initial = await registry.snapshot(route_id, observed_at=NOW)
    consecutive_failures = (
        0 if status is ProviderHealthStatus.HEALTHY else 1
    )
    snapshot = build_provider_health_snapshot(
        route_id=route_id,
        status=status,
        reason=f"Synthetic routing drill status: {status.value}.",
        observed_at=NOW + timedelta(seconds=1),
        valid_until=NOW + timedelta(seconds=31),
        consecutive_failures=consecutive_failures,
        latency_p95_ms=20,
        previous_snapshot_sha256=initial.snapshot_sha256,
    )
    await registry.record(snapshot)


async def run_probe(config_path: Path) -> dict[str, object]:
    loaded = await load_provider_configuration(config_path)
    route_ids = tuple(
        route.route_id
        for route in loaded.configuration.routes
        if route.enabled
    )
    health_registry = ProviderHealthRegistry(route_ids, observed_at=NOW)
    for route_id in route_ids:
        status = (
            ProviderHealthStatus.UNAVAILABLE
            if route_id == "configured.secondary"
            else ProviderHealthStatus.HEALTHY
        )
        await _record_health(health_registry, route_id, status)

    requirements = ProviderRequirements(
        input_tokens=1_000,
        reserved_output_tokens=500,
        required_capabilities=(),
        required_input_modalities=("text",),
        required_output_modalities=("text",),
        required_context_features=("prompt_cache",),
        data_classification=DataClassification.CONFIDENTIAL,
        allowed_regions=("us-east-1",),
        max_retention_days=0,
        allow_training=False,
        max_cost_microusd=2_500,
    )
    inventory = ConfiguredRouteInventory(loaded, health_registry)
    candidates = await inventory.candidates(
        requirements,
        observed_at=NOW + timedelta(seconds=2),
    )
    decision = select_provider_route(
        provider_decision_id="pvd_" + "1" * 32,
        turn_id="trn_" + "2" * 32,
        requirements=requirements,
        candidates=candidates,
        decided_at=NOW + timedelta(seconds=2),
    )
    decision_json = decision.model_dump_json()
    return {
        "candidate_count": len(candidates),
        "configuration_sha256": loaded.content_sha256,
        "decision_sha256": hashlib.sha256(decision_json.encode()).hexdigest(),
        "eligible_routes": tuple(
            route.route_id for route in decision.eligible_routes
        ),
        "rejected_routes": {
            route.route_id: {
                "codes": tuple(
                    code.value for code in route.rejection_codes
                ),
                "price_version_sha256": route.price_version_sha256,
            }
            for route in decision.rejected_routes
        },
        "secret_free": (
            "pcr_" not in decision_json
            and "credential" not in decision_json
            and "api_key" not in decision_json
        ),
        "selected_route_id": decision.selected_route_id,
        "selection_reason": decision.selection_reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-path", required=True, type=Path)
    arguments = parser.parse_args()
    report = asyncio.run(run_probe(arguments.config_path))
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
