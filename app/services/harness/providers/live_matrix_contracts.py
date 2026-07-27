"""Bounded, redacted cross-provider live-evidence contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    ProviderName,
    ProviderTokenUsage,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.base import BoundedReason
from app.services.harness.protocol.routing import ModelName
from app.services.harness.providers.conformance_contracts import (
    MAXIMUM_CONFORMANCE_ADAPTERS,
    MAXIMUM_CONFORMANCE_SCENARIOS,
    ConformanceScenario,
)

MAXIMUM_LIVE_MATRIX_COST_MICROUSD = 10_000_000_000
MAXIMUM_LIVE_OBSERVATIONS = (
    MAXIMUM_CONFORMANCE_ADAPTERS * MAXIMUM_CONFORMANCE_SCENARIOS
)
REQUIRED_LIVE_SCENARIOS = (
    ConformanceScenario.CANCELLATION,
    ConformanceScenario.TEXT_STREAM,
    ConformanceScenario.USAGE_COST,
)


class LiveProviderEnvironment(StrEnum):
    CLOUD = "cloud"
    LOCAL = "local"
    MOCK = "mock"


class LiveEvidenceStatus(StrEnum):
    FAILED = "failed"
    PASSED = "passed"
    UNSUPPORTED = "unsupported"
    UNTESTED = "untested"


class LiveEvidenceFailureCode(StrEnum):
    ADMISSION = "admission"
    AUTHENTICATION = "authentication"
    BUDGET = "budget"
    CONTRACT = "contract"
    EXECUTION = "execution"
    TIMEOUT = "timeout"


class LiveProviderDescriptor(StrictProtocolModel):
    provider: ProviderName
    environment: LiveProviderEnvironment
    model: ModelName
    model_revision_sha256: Sha256
    adapter_revision_sha256: Sha256
    route_binding_sha256: Sha256
    supported_scenarios: tuple[ConformanceScenario, ...] = Field(
        max_length=MAXIMUM_CONFORMANCE_SCENARIOS,
    )
    observed_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_scenarios(self) -> Self:
        if (
            tuple(sorted(set(self.supported_scenarios)))
            != self.supported_scenarios
        ):
            raise ValueError("live provider scenarios must be canonical")
        return self


class LiveEvidenceObservation(StrictProtocolModel):
    provider: ProviderName
    model_revision_sha256: Sha256
    adapter_revision_sha256: Sha256
    route_binding_sha256: Sha256
    scenario: ConformanceScenario
    status: LiveEvidenceStatus
    latency_ms: int | None = Field(
        default=None,
        ge=0,
        le=3_600_000,
    )
    cancellation_latency_ms: int | None = Field(
        default=None,
        ge=0,
        le=60_000,
    )
    usage: ProviderTokenUsage | None = None
    charged_cost_microusd: int | None = Field(
        default=None,
        ge=0,
        le=MAXIMUM_LIVE_MATRIX_COST_MICROUSD,
    )
    trace_sha256: Sha256 | None = None
    failure_code: LiveEvidenceFailureCode | None = None
    reason: BoundedReason
    observed_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_status_evidence(self) -> Self:
        has_execution_evidence = (
            self.latency_ms is not None
            or self.cancellation_latency_ms is not None
            or self.usage is not None
            or self.charged_cost_microusd is not None
            or self.trace_sha256 is not None
            or self.failure_code is not None
        )
        if self.status in {
            LiveEvidenceStatus.UNTESTED,
            LiveEvidenceStatus.UNSUPPORTED,
        }:
            if has_execution_evidence:
                raise ValueError(
                    "unexecuted live evidence contains runtime measurements"
                )
            return self
        if (
            self.latency_ms is None
            or self.trace_sha256 is None
            or (self.status is LiveEvidenceStatus.FAILED)
            != (self.failure_code is not None)
        ):
            raise ValueError("executed live evidence is incomplete")
        if self.status is LiveEvidenceStatus.PASSED:
            is_cancellation = (
                self.scenario is ConformanceScenario.CANCELLATION
            )
            if is_cancellation != (
                self.cancellation_latency_ms is not None
            ):
                raise ValueError(
                    "live cancellation evidence is inconsistent"
                )
            has_usage = self.usage is not None
            has_cost = self.charged_cost_microusd is not None
            if has_usage != has_cost:
                raise ValueError("live usage and cost evidence is incomplete")
            if (
                self.scenario is ConformanceScenario.USAGE_COST
                and not has_usage
            ):
                raise ValueError("live usage scenario has no usage evidence")
        return self


class LiveConformanceMatrix(StrictProtocolModel):
    scenarios: tuple[ConformanceScenario, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_CONFORMANCE_SCENARIOS,
    )
    providers: tuple[LiveProviderDescriptor, ...] = Field(
        min_length=3,
        max_length=MAXIMUM_CONFORMANCE_ADAPTERS,
    )
    required_live_providers: tuple[ProviderName, ...] = Field(
        min_length=3,
        max_length=MAXIMUM_CONFORMANCE_ADAPTERS,
    )
    observations: tuple[LiveEvidenceObservation, ...] = Field(
        min_length=3,
        max_length=MAXIMUM_LIVE_OBSERVATIONS,
    )
    authorized_cost_cap_microusd: int = Field(
        ge=1,
        le=MAXIMUM_LIVE_MATRIX_COST_MICROUSD,
    )
    charged_cost_microusd: int = Field(
        ge=0,
        le=MAXIMUM_LIVE_MATRIX_COST_MICROUSD,
    )
    release_ready: bool
    generated_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_matrix(self) -> Self:
        self._validate_canonical_dimensions()
        descriptors = {
            descriptor.provider: descriptor
            for descriptor in self.providers
        }
        expected_keys = tuple(
            (scenario, provider)
            for scenario in self.scenarios
            for provider in descriptors
        )
        actual_keys = tuple(
            (observation.scenario, observation.provider)
            for observation in self.observations
        )
        if actual_keys != expected_keys:
            raise ValueError("live matrix observations are incomplete")
        for observation in self.observations:
            self._validate_observation(
                observation,
                descriptors[observation.provider],
            )
        total_cost = sum(
            observation.charged_cost_microusd or 0
            for observation in self.observations
        )
        if (
            total_cost != self.charged_cost_microusd
            or total_cost > self.authorized_cost_cap_microusd
        ):
            raise ValueError("live matrix cost evidence is inconsistent")
        statuses = {
            (observation.provider, observation.scenario): observation.status
            for observation in self.observations
        }
        expected_ready = all(
            statuses[(provider, scenario)] is LiveEvidenceStatus.PASSED
            for provider in self.required_live_providers
            for scenario in REQUIRED_LIVE_SCENARIOS
        )
        if self.release_ready != expected_ready:
            raise ValueError("live matrix release state is inconsistent")
        return self

    def _validate_canonical_dimensions(self) -> None:
        if tuple(sorted(set(self.scenarios))) != self.scenarios:
            raise ValueError("live matrix scenarios must be canonical")
        providers = tuple(
            descriptor.provider for descriptor in self.providers
        )
        if tuple(sorted(set(providers))) != providers:
            raise ValueError("live matrix providers must be canonical")
        if (
            tuple(sorted(set(self.required_live_providers)))
            != self.required_live_providers
            or not set(self.required_live_providers).issubset(providers)
        ):
            raise ValueError("required live providers are invalid")
        required_descriptors = tuple(
            descriptor
            for descriptor in self.providers
            if descriptor.provider in self.required_live_providers
        )
        cloud_count = sum(
            descriptor.environment is LiveProviderEnvironment.CLOUD
            for descriptor in required_descriptors
        )
        local_count = sum(
            descriptor.environment is LiveProviderEnvironment.LOCAL
            for descriptor in required_descriptors
        )
        if cloud_count < 2 or local_count < 1:
            raise ValueError(
                "release evidence requires two cloud providers and one local"
            )
        if any(
            not set(REQUIRED_LIVE_SCENARIOS).issubset(
                descriptor.supported_scenarios
            )
            for descriptor in required_descriptors
        ):
            raise ValueError("required provider lacks a live release scenario")

    def _validate_observation(
        self,
        observation: LiveEvidenceObservation,
        descriptor: LiveProviderDescriptor,
    ) -> None:
        if (
            observation.model_revision_sha256
            != descriptor.model_revision_sha256
            or observation.adapter_revision_sha256
            != descriptor.adapter_revision_sha256
            or observation.route_binding_sha256
            != descriptor.route_binding_sha256
            or observation.observed_at < descriptor.observed_at
            or observation.observed_at > self.generated_at
        ):
            raise ValueError("live observation revision evidence is invalid")
        supported = observation.scenario in descriptor.supported_scenarios
        if supported == (
            observation.status is LiveEvidenceStatus.UNSUPPORTED
        ):
            raise ValueError("live scenario support status is inconsistent")
