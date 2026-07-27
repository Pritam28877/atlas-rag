"""Fail-closed construction of complete live conformance matrices."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from app.services.harness.protocol import ProviderName
from app.services.harness.providers.conformance_contracts import (
    CONFORMANCE_SCENARIOS,
    ConformanceScenario,
)
from app.services.harness.providers.live_matrix_contracts import (
    REQUIRED_LIVE_SCENARIOS,
    LiveConformanceMatrix,
    LiveEvidenceObservation,
    LiveEvidenceStatus,
    LiveProviderDescriptor,
)


def build_live_conformance_matrix(
    providers: Sequence[LiveProviderDescriptor],
    executed_observations: Sequence[LiveEvidenceObservation],
    *,
    required_live_providers: Sequence[ProviderName],
    authorized_cost_cap_microusd: int,
    generated_at: datetime,
) -> LiveConformanceMatrix:
    canonical_providers = tuple(
        sorted(providers, key=_provider_name)
    )
    observed = _indexed_observations(executed_observations)
    observations: list[LiveEvidenceObservation] = []
    for scenario in CONFORMANCE_SCENARIOS:
        for descriptor in canonical_providers:
            key = (scenario.value, descriptor.provider)
            observation = observed.pop(key, None)
            observations.append(
                observation
                if observation is not None
                else _unexecuted(descriptor, scenario, generated_at)
            )
    if observed:
        raise ValueError("live observations contain unknown matrix entries")
    total_cost = sum(
        observation.charged_cost_microusd or 0
        for observation in observations
    )
    required = tuple(sorted(required_live_providers))
    release_ready = all(
        observation.status is LiveEvidenceStatus.PASSED
        for observation in observations
        if observation.provider in required
        and observation.scenario in REQUIRED_LIVE_SCENARIOS
    )
    return LiveConformanceMatrix(
        scenarios=CONFORMANCE_SCENARIOS,
        providers=canonical_providers,
        required_live_providers=required,
        observations=tuple(observations),
        authorized_cost_cap_microusd=authorized_cost_cap_microusd,
        charged_cost_microusd=total_cost,
        release_ready=release_ready,
        generated_at=generated_at,
    )


def _indexed_observations(
    observations: Sequence[LiveEvidenceObservation],
) -> dict[tuple[str, str], LiveEvidenceObservation]:
    indexed: dict[tuple[str, str], LiveEvidenceObservation] = {}
    for observation in observations:
        key = (observation.scenario.value, observation.provider)
        if key in indexed:
            raise ValueError("live observations contain duplicate entries")
        indexed[key] = observation
    return indexed


def _provider_name(descriptor: LiveProviderDescriptor) -> str:
    return descriptor.provider


def _unexecuted(
    descriptor: LiveProviderDescriptor,
    scenario: ConformanceScenario,
    observed_at: datetime,
) -> LiveEvidenceObservation:
    supported = scenario in descriptor.supported_scenarios
    return LiveEvidenceObservation(
        provider=descriptor.provider,
        model_revision_sha256=descriptor.model_revision_sha256,
        adapter_revision_sha256=descriptor.adapter_revision_sha256,
        route_binding_sha256=descriptor.route_binding_sha256,
        scenario=scenario,
        status=(
            LiveEvidenceStatus.UNTESTED
            if supported
            else LiveEvidenceStatus.UNSUPPORTED
        ),
        reason=(
            "Live scenario has not been executed."
            if supported
            else "Scenario is unsupported by this adapter revision."
        ),
        observed_at=observed_at,
    )
