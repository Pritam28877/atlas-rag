"""Prepare a private live-matrix manifest from conformance evidence."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from app.cli.harness.live_matrix_io import (
    LiveMatrixManifest,
    LiveSmokeEvidenceReference,
)
from app.cli.harness.provider_smoke_io import (
    MAXIMUM_SMOKE_POLICY_BYTES,
    read_private_smoke_input,
    write_private_smoke_output,
)
from app.services.harness.protocol import (
    ProviderName,
    StrictProtocolModel,
)
from app.services.harness.providers.conformance_contracts import (
    MAXIMUM_CONFORMANCE_ADAPTERS,
    ConformanceReport,
)
from app.services.harness.providers.live_matrix_contracts import (
    MAXIMUM_LIVE_MATRIX_COST_MICROUSD,
)
from app.services.harness.providers.live_matrix_descriptors import (
    LiveProviderTarget,
    derive_live_provider_descriptors,
)


class LiveMatrixPreparationRequest(StrictProtocolModel):
    schema_version: Literal[1] = 1
    targets: tuple[LiveProviderTarget, ...] = Field(
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
        le=MAXIMUM_LIVE_MATRIX_COST_MICROUSD,
    )
    result_path: Path


async def prepare_live_matrix_manifest(
    conformance_report_path: Path,
    preparation_path: Path,
    manifest_path: Path,
) -> LiveMatrixManifest:
    _validate_control_paths(
        conformance_report_path,
        preparation_path,
        manifest_path,
    )
    report_content, preparation_content = await asyncio.gather(
        read_private_smoke_input(
            conformance_report_path,
            maximum_bytes=MAXIMUM_SMOKE_POLICY_BYTES,
        ),
        read_private_smoke_input(
            preparation_path,
            maximum_bytes=MAXIMUM_SMOKE_POLICY_BYTES,
        ),
    )
    try:
        report = ConformanceReport.model_validate_json(report_content)
        preparation = LiveMatrixPreparationRequest.model_validate_json(
            preparation_content
        )
    except ValidationError:
        raise ValueError(
            "live matrix preparation input is invalid"
        ) from None
    providers = derive_live_provider_descriptors(
        report,
        preparation.targets,
    )
    manifest = LiveMatrixManifest(
        providers=providers,
        evidence=preparation.evidence,
        required_live_providers=preparation.required_live_providers,
        authorized_cost_cap_microusd=(
            preparation.authorized_cost_cap_microusd
        ),
        result_path=preparation.result_path,
    )
    _validate_manifest_paths(
        conformance_report_path,
        preparation_path,
        manifest_path,
        manifest,
    )
    await write_private_smoke_output(
        manifest_path,
        manifest.model_dump_json().encode(),
    )
    return manifest


def _validate_control_paths(*paths: Path) -> None:
    if any(not path.is_absolute() for path in paths):
        raise ValueError("live matrix preparation paths must be absolute")
    if len(set(paths)) != len(paths):
        raise ValueError("live matrix preparation paths must be distinct")


def _validate_manifest_paths(
    conformance_report_path: Path,
    preparation_path: Path,
    manifest_path: Path,
    manifest: LiveMatrixManifest,
) -> None:
    paths = (
        conformance_report_path,
        preparation_path,
        manifest_path,
        manifest.result_path,
        *(reference.result_path for reference in manifest.evidence),
    )
    if len(set(paths)) != len(paths):
        raise ValueError("live matrix preparation paths collide")
