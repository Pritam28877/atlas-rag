"""Exercise real environment resolution through the configured broker."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services.harness.providers import (
    ConfiguredCredentialBroker,
    EnvironmentCredentialBackend,
    EnvironmentCredentialReference,
    load_provider_configuration,
)

NOW = datetime(2026, 7, 29, 11, 0, tzinfo=UTC)


async def run_probe(
    *,
    config_path: Path,
    handle: str,
    provider: str,
    destination_sha256: str,
    environment_variable: str,
) -> dict[str, object]:
    loaded = await load_provider_configuration(config_path)
    backend = EnvironmentCredentialBackend(
        (
            EnvironmentCredentialReference(
                handle=handle,
                environment_variable=environment_variable,
                lease_ttl_seconds=60,
            ),
        ),
        development_mode=True,
        clock=lambda: NOW,
    )
    broker = ConfiguredCredentialBroker(
        loaded,
        backend,
        clock=lambda: NOW,
    )
    lease = await broker.acquire(
        handle,
        provider=provider,
        destination_sha256=destination_sha256,
        cancellation=asyncio.Event(),
        deadline_at=NOW + timedelta(seconds=1),
    )
    secret_view = lease.secret_view()
    material_present = len(secret_view) > 0
    representation_redacted = "<redacted>" in repr(lease)
    await broker.release(lease)
    return {
        "active_leases": await broker.active_leases(),
        "lease_released": lease.released,
        "material_present": material_present,
        "material_zeroed": bytes(secret_view) == bytes(len(secret_view)),
        "representation_redacted": representation_redacted,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-path", required=True, type=Path)
    parser.add_argument("--handle", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--destination-sha256", required=True)
    parser.add_argument("--environment-variable", required=True)
    arguments = parser.parse_args()
    report = asyncio.run(
        run_probe(
            config_path=arguments.config_path,
            handle=arguments.handle,
            provider=arguments.provider,
            destination_sha256=arguments.destination_sha256,
            environment_variable=arguments.environment_variable,
        )
    )
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
