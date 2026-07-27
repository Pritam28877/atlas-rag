"""Redacted, revision-bound live-smoke failure evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from app.cli.harness.provider_smoke_io import write_private_smoke_output
from app.cli.harness.smoke_timing import MAXIMUM_SMOKE_LATENCY_MS
from app.services.harness.protocol import (
    ProviderName,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.routing import ModelName
from app.services.harness.providers.conformance_contracts import (
    MAXIMUM_CONFORMANCE_SCENARIOS,
    ConformanceScenario,
)
from app.services.harness.providers.live_matrix_contracts import (
    MAXIMUM_LIVE_MATRIX_COST_MICROUSD,
    LiveEvidenceFailureCode,
    LiveEvidenceObservation,
    LiveEvidenceStatus,
    LiveProviderDescriptor,
)


class LiveSmokeFailureReceipt(StrictProtocolModel):
    schema_version: Literal[1] = 1
    provider: ProviderName
    model: ModelName
    model_revision_sha256: Sha256
    adapter_revision_sha256: Sha256
    route_binding_sha256: Sha256
    scenarios: tuple[ConformanceScenario, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_CONFORMANCE_SCENARIOS,
    )
    failure_code: LiveEvidenceFailureCode
    latency_ms: int = Field(ge=0, le=MAXIMUM_SMOKE_LATENCY_MS)
    charged_cost_microusd: int = Field(
        ge=0,
        le=MAXIMUM_LIVE_MATRIX_COST_MICROUSD,
    )
    authorized_cost_cap_microusd: int = Field(
        ge=0,
        le=MAXIMUM_LIVE_MATRIX_COST_MICROUSD,
    )
    trace_sha256: Sha256
    occurred_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_failure_evidence(self) -> Self:
        if tuple(sorted(set(self.scenarios))) != self.scenarios:
            raise ValueError("live failure scenarios must be canonical")
        if (
            self.charged_cost_microusd
            > self.authorized_cost_cap_microusd
        ):
            raise ValueError("live failure cost exceeds its authorized cap")
        return self


def failed_live_observations(
    descriptor: LiveProviderDescriptor,
    receipt: LiveSmokeFailureReceipt,
) -> tuple[LiveEvidenceObservation, ...]:
    if (
        receipt.provider != descriptor.provider
        or receipt.model != descriptor.model
        or receipt.model_revision_sha256
        != descriptor.model_revision_sha256
        or receipt.adapter_revision_sha256
        != descriptor.adapter_revision_sha256
        or receipt.route_binding_sha256
        != descriptor.route_binding_sha256
        or receipt.occurred_at < descriptor.observed_at
    ):
        raise ValueError("live failure descriptor binding is invalid")
    if not set(receipt.scenarios).issubset(
        descriptor.supported_scenarios
    ):
        raise ValueError("live failure claims an unsupported scenario")
    observations: list[LiveEvidenceObservation] = []
    for index, scenario in enumerate(receipt.scenarios):
        observations.append(
            LiveEvidenceObservation(
                provider=descriptor.provider,
                model_revision_sha256=(
                    descriptor.model_revision_sha256
                ),
                adapter_revision_sha256=(
                    descriptor.adapter_revision_sha256
                ),
                route_binding_sha256=(
                    descriptor.route_binding_sha256
                ),
                scenario=scenario,
                status=LiveEvidenceStatus.FAILED,
                latency_ms=receipt.latency_ms,
                charged_cost_microusd=(
                    receipt.charged_cost_microusd
                    if index == 0
                    else None
                ),
                trace_sha256=receipt.trace_sha256,
                failure_code=receipt.failure_code,
                reason="Authorized live smoke evidence failed.",
                observed_at=receipt.occurred_at,
            )
        )
    return tuple(observations)


async def write_live_smoke_failure_receipt(
    path: Path,
    receipt: LiveSmokeFailureReceipt,
) -> None:
    await write_private_smoke_output(
        path,
        receipt.model_dump_json().encode(),
    )
