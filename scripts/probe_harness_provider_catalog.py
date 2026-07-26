"""Load real provider metadata and compose bounded catalog plus health."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services.harness.protocol import (
    ProviderCatalogPageRequest,
    ProviderHealthStatus,
    ProviderListPageRequest,
    build_provider_health_snapshot,
)
from app.services.harness.providers import (
    ConfiguredModelCatalog,
    ProviderHealthRegistry,
    load_provider_configuration,
)

NOW = datetime(2026, 7, 28, 13, 0, tzinfo=UTC)


async def run_probe(config_path: Path) -> dict[str, object]:
    loaded = await load_provider_configuration(config_path)
    catalog = ConfiguredModelCatalog(loaded, clock=lambda: NOW)
    cancellation = asyncio.Event()
    provider_page = await catalog.list_providers(
        ProviderListPageRequest(limit=64),
        cancellation=cancellation,
        deadline_at=NOW + timedelta(seconds=1),
    )
    model_pages: dict[str, dict[str, object]] = {}
    catalog_pages_secret_free = "pcr_" not in provider_page.model_dump_json()
    for summary in provider_page.providers:
        model_page = await catalog.page(
            ProviderCatalogPageRequest(
                provider=summary.provider,
                limit=200,
            ),
            cancellation=cancellation,
            deadline_at=NOW + timedelta(seconds=1),
        )
        catalog_pages_secret_free = (
            catalog_pages_secret_free
            and "pcr_" not in model_page.model_dump_json()
            and "destination" not in model_page.model_dump_json()
        )
        model_pages[summary.provider] = {
            "first_page_count": len(model_page.models),
            "has_more": model_page.has_more,
            "snapshot_matches": (
                model_page.catalog_snapshot_sha256
                == summary.catalog_snapshot_sha256
            ),
        }

    route_ids = tuple(
        route.route_id
        for route in loaded.configuration.routes
        if route.enabled
    )
    health = ProviderHealthRegistry(route_ids, observed_at=NOW)
    initial = await health.snapshot(route_ids[0], observed_at=NOW)
    healthy = build_provider_health_snapshot(
        route_id=route_ids[0],
        status=ProviderHealthStatus.HEALTHY,
        reason="Synthetic bounded provider health probe succeeded.",
        observed_at=NOW + timedelta(seconds=1),
        valid_until=NOW + timedelta(seconds=31),
        consecutive_failures=0,
        latency_p95_ms=10,
        previous_snapshot_sha256=initial.snapshot_sha256,
    )
    await health.record(healthy)
    current = await health.snapshot(
        route_ids[0],
        observed_at=NOW + timedelta(seconds=2),
    )
    return {
        "catalog_pages_secret_free": catalog_pages_secret_free,
        "configuration_sha256": loaded.content_sha256,
        "health": {
            "configured_routes": await health.size(),
            "lineage_applied": (
                current.previous_snapshot_sha256
                == initial.snapshot_sha256
            ),
            "status": current.status.value,
        },
        "model_pages": model_pages,
        "provider_page": {
            "has_more": provider_page.has_more,
            "providers": tuple(
                summary.provider for summary in provider_page.providers
            ),
        },
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
