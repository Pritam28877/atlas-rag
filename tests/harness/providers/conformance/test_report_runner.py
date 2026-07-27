import asyncio
import stat
from pathlib import Path

import pytest

import app.cli.harness.conformance_report_runner as report_runner
from app.cli.harness.conformance_report_runner import (
    PackagedConformanceReportRequest,
    run_packaged_conformance_report,
)
from app.cli.harness.provider_smoke_io import (
    MAXIMUM_SMOKE_POLICY_BYTES,
)
from app.services.harness.providers.conformance_contracts import (
    ConformanceCaseStatus,
    ConformanceComparisonStatus,
)
from tests.harness.providers.conformance.fixtures import NOW


def test_runner_writes_private_redacted_passing_report(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    result_path = tmp_path / "conformance.json"
    request = PackagedConformanceReportRequest(
        timeout_seconds=30,
        result_path=result_path,
    )

    report = asyncio.run(
        run_packaged_conformance_report(
            request,
            clock=lambda: NOW,
        )
    )

    persisted = result_path.read_text()
    assert len(report.adapters) == 6
    assert len(report.observations) == 48
    assert all(
        observation.status is ConformanceCaseStatus.PASSED
        for observation in report.observations
    )
    assert all(
        comparison.status
        is ConformanceComparisonStatus.EQUIVALENT
        for comparison in report.comparisons
    )
    assert stat.S_IMODE(result_path.stat().st_mode) == 0o600
    assert result_path.stat().st_size <= MAXIMUM_SMOKE_POLICY_BYTES
    assert "Atlas conformance." not in persisted
    assert "redacted fixture detail" not in persisted
    assert "HARM_CATEGORY" not in persisted


def test_runner_writes_nothing_when_quality_gate_rejects(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tmp_path.chmod(0o700)
    result_path = tmp_path / "conformance.json"
    request = PackagedConformanceReportRequest(
        timeout_seconds=30,
        result_path=result_path,
    )

    def reject(_report) -> None:
        raise ValueError("report quality rejected")

    monkeypatch.setattr(
        report_runner,
        "require_live_descriptor_evidence",
        reject,
    )

    with pytest.raises(ValueError, match="quality"):
        asyncio.run(
            run_packaged_conformance_report(
                request,
                clock=lambda: NOW,
            )
        )

    assert not result_path.exists()
