"""Run and persist the packaged offline provider conformance suite."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import Field

from app.cli.harness.provider_smoke_io import (
    MAXIMUM_SMOKE_POLICY_BYTES,
    validate_provider_smoke_output_path,
    write_private_smoke_output,
)
from app.services.harness.protocol import StrictProtocolModel
from app.services.harness.providers.conformance_contracts import (
    ConformanceReport,
)
from app.services.harness.providers.conformance_runner import (
    BoundedConformanceRunner,
)
from app.services.harness.providers.conformance_suite import (
    recorded_conformance_adapters,
)
from app.services.harness.providers.live_matrix_descriptors import (
    require_live_descriptor_evidence,
)

Clock = Callable[[], datetime]


class PackagedConformanceReportRequest(StrictProtocolModel):
    timeout_seconds: int = Field(ge=1, le=300)
    result_path: Path


async def run_packaged_conformance_report(
    request: PackagedConformanceReportRequest,
    *,
    clock: Clock | None = None,
) -> ConformanceReport:
    if not request.result_path.is_absolute():
        raise ValueError("conformance report path must be absolute")
    await validate_provider_smoke_output_path(request.result_path)
    runtime_clock = clock or _utc_now
    started_at = runtime_clock()
    adapters = recorded_conformance_adapters(clock=runtime_clock)
    report = await BoundedConformanceRunner(
        clock=runtime_clock,
        per_case_timeout_seconds=min(
            5.0,
            float(request.timeout_seconds),
        ),
    ).run(
        adapters,
        cancellation=asyncio.Event(),
        deadline_at=started_at
        + timedelta(seconds=request.timeout_seconds),
    )
    require_live_descriptor_evidence(report)
    content = report.model_dump_json().encode()
    if len(content) > MAXIMUM_SMOKE_POLICY_BYTES:
        raise ValueError("conformance report exceeds its size limit")
    await write_private_smoke_output(request.result_path, content)
    return report


def _utc_now() -> datetime:
    return datetime.now(UTC)
