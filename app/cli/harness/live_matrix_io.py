"""Private bounded I/O for cross-provider live matrices."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from app.cli.harness.adapter_smoke_io import AdapterSmokeResult
from app.cli.harness.bedrock_smoke_io import BedrockSmokeResult
from app.cli.harness.live_matrix_smoke import (
    LiveSmokeResult,
    live_observations_from_smoke,
)
from app.cli.harness.provider_smoke_io import (
    MAXIMUM_SMOKE_POLICY_BYTES,
    ProviderSmokeResult,
    read_private_smoke_input,
    validate_provider_smoke_output_path,
    write_private_smoke_output,
)
from app.services.harness.protocol import (
    ProviderName,
    StrictProtocolModel,
)
from app.services.harness.providers.conformance_contracts import (
    MAXIMUM_CONFORMANCE_ADAPTERS,
)
from app.services.harness.providers.live_matrix_builder import (
    build_live_conformance_matrix,
)
from app.services.harness.providers.live_matrix_contracts import (
    LiveConformanceMatrix,
    LiveEvidenceObservation,
    LiveProviderDescriptor,
)

MAXIMUM_LIVE_MATRIX_OUTPUT_BYTES = 1024 * 1024
Clock = Callable[[], datetime]


class LiveSmokeResultKind(StrEnum):
    ADAPTER = "adapter"
    BEDROCK = "bedrock"
    PROVIDER = "provider"


class LiveSmokeEvidenceReference(StrictProtocolModel):
    provider: ProviderName
    kind: LiveSmokeResultKind
    result_path: Path


class LiveMatrixManifest(StrictProtocolModel):
    schema_version: Literal[1] = 1
    providers: tuple[LiveProviderDescriptor, ...] = Field(
        min_length=3,
        max_length=MAXIMUM_CONFORMANCE_ADAPTERS,
    )
    evidence: tuple[LiveSmokeEvidenceReference, ...] = Field(
        max_length=MAXIMUM_CONFORMANCE_ADAPTERS,
    )
    required_live_providers: tuple[ProviderName, ...] = Field(
        min_length=3,
        max_length=MAXIMUM_CONFORMANCE_ADAPTERS,
    )
    authorized_cost_cap_microusd: int = Field(
        ge=1,
        le=10_000_000_000,
    )
    result_path: Path

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        providers = tuple(
            descriptor.provider for descriptor in self.providers
        )
        evidence_providers = tuple(
            reference.provider for reference in self.evidence
        )
        if tuple(sorted(set(providers))) != providers:
            raise ValueError("live matrix manifest providers are not canonical")
        if (
            tuple(sorted(set(evidence_providers)))
            != evidence_providers
            or not set(evidence_providers).issubset(providers)
        ):
            raise ValueError("live matrix evidence providers are invalid")
        paths = tuple(
            reference.result_path for reference in self.evidence
        )
        if (
            not self.result_path.is_absolute()
            or any(not path.is_absolute() for path in paths)
            or len(set((*paths, self.result_path)))
            != len(paths) + 1
        ):
            raise ValueError("live matrix paths are invalid")
        return self


async def build_live_matrix_from_manifest(
    manifest_path: Path,
    *,
    clock: Clock | None = None,
) -> LiveConformanceMatrix:
    if not manifest_path.is_absolute():
        raise ValueError("live matrix manifest path must be absolute")
    manifest = await _load_manifest(manifest_path)
    if manifest.result_path == manifest_path:
        raise ValueError("live matrix output cannot replace its manifest")
    await validate_provider_smoke_output_path(manifest.result_path)
    results = await asyncio.gather(
        *(_load_result(reference) for reference in manifest.evidence)
    )
    descriptors = {
        descriptor.provider: descriptor
        for descriptor in manifest.providers
    }
    observations: list[LiveEvidenceObservation] = []
    for reference, result in zip(
        manifest.evidence,
        results,
        strict=True,
    ):
        observations.extend(
            live_observations_from_smoke(
                descriptors[reference.provider],
                result,
            )
        )
    generated_at = (clock or _utc_now)()
    matrix = build_live_conformance_matrix(
        manifest.providers,
        observations,
        required_live_providers=manifest.required_live_providers,
        authorized_cost_cap_microusd=(
            manifest.authorized_cost_cap_microusd
        ),
        generated_at=generated_at,
    )
    content = matrix.model_dump_json().encode()
    if len(content) > MAXIMUM_LIVE_MATRIX_OUTPUT_BYTES:
        raise ValueError("live matrix output exceeds its size limit")
    await write_private_smoke_output(manifest.result_path, content)
    return matrix


async def _load_manifest(path: Path) -> LiveMatrixManifest:
    content = await read_private_smoke_input(
        path,
        maximum_bytes=MAXIMUM_SMOKE_POLICY_BYTES,
    )
    try:
        return LiveMatrixManifest.model_validate_json(content)
    except ValidationError:
        raise ValueError("live matrix manifest is invalid") from None


async def _load_result(
    reference: LiveSmokeEvidenceReference,
) -> LiveSmokeResult:
    content = await read_private_smoke_input(
        reference.result_path,
        maximum_bytes=MAXIMUM_SMOKE_POLICY_BYTES,
    )
    result_model: (
        type[AdapterSmokeResult]
        | type[BedrockSmokeResult]
        | type[ProviderSmokeResult]
    )
    if reference.kind is LiveSmokeResultKind.ADAPTER:
        result_model = AdapterSmokeResult
    elif reference.kind is LiveSmokeResultKind.BEDROCK:
        result_model = BedrockSmokeResult
    else:
        result_model = ProviderSmokeResult
    try:
        result = result_model.model_validate_json(content)
    except ValidationError:
        raise ValueError("live smoke result is invalid") from None
    if result.provider != reference.provider:
        raise ValueError("live smoke result provider is invalid")
    return result


def _utc_now() -> datetime:
    return datetime.now(UTC)
