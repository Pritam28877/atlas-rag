"""Derive live provider descriptors from verified conformance evidence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from app.services.harness.protocol import (
    ProviderName,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.routing import ModelName
from app.services.harness.providers.conformance_contracts import (
    MAXIMUM_CONFORMANCE_ADAPTERS,
    ConformanceReport,
)
from app.services.harness.providers.live_matrix_contracts import (
    LiveProviderDescriptor,
    LiveProviderEnvironment,
)


class LiveProviderTarget(StrictProtocolModel):
    provider: ProviderName
    environment: LiveProviderEnvironment
    model: ModelName
    model_revision_sha256: Sha256
    route_binding_sha256: Sha256
    observed_at: UtcTimestamp


def derive_live_provider_descriptors(
    report: ConformanceReport,
    targets: Sequence[LiveProviderTarget],
) -> tuple[LiveProviderDescriptor, ...]:
    if not 1 <= len(targets) <= MAXIMUM_CONFORMANCE_ADAPTERS:
        raise ValueError("live provider target count is invalid")
    canonical_targets = tuple(sorted(targets, key=_target_provider))
    target_providers = tuple(
        target.provider for target in canonical_targets
    )
    report_providers = tuple(
        adapter.provider for adapter in report.adapters
    )
    if (
        tuple(sorted(set(target_providers))) != target_providers
        or target_providers != report_providers
    ):
        raise ValueError(
            "live provider targets do not match conformance adapters"
        )
    targets_by_provider = {
        target.provider: target for target in canonical_targets
    }
    descriptors: list[LiveProviderDescriptor] = []
    for adapter in report.adapters:
        target = targets_by_provider[adapter.provider]
        descriptors.append(
            LiveProviderDescriptor(
                provider=adapter.provider,
                environment=target.environment,
                model=target.model,
                model_revision_sha256=(
                    target.model_revision_sha256
                ),
                adapter_revision_sha256=(
                    adapter.adapter_revision_sha256
                ),
                route_binding_sha256=(
                    target.route_binding_sha256
                ),
                supported_scenarios=adapter.supported_scenarios,
                observed_at=_latest(
                    report.observed_at,
                    target.observed_at,
                ),
            )
        )
    return tuple(descriptors)


def _target_provider(target: LiveProviderTarget) -> str:
    return target.provider


def _latest(first: datetime, second: datetime) -> datetime:
    return max(first, second)
