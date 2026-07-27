import pytest
from pydantic import ValidationError

from app.cli.harness.live_matrix_failure import (
    LiveSmokeFailureReceipt,
    failed_live_observations,
)
from app.services.harness.providers.conformance_contracts import (
    ConformanceScenario,
)
from app.services.harness.providers.live_matrix_contracts import (
    LiveEvidenceFailureCode,
    LiveEvidenceStatus,
)
from tests.harness.providers.live_matrix.fixtures import descriptors
from tests.harness.providers.live_matrix.smoke_fixtures import (
    failure_receipt,
)


def test_failure_receipt_maps_cost_once_without_raw_trace() -> None:
    observations = failed_live_observations(
        descriptors()[-1],
        failure_receipt(),
    )

    assert tuple(item.scenario for item in observations) == (
        ConformanceScenario.TEXT_STREAM,
        ConformanceScenario.USAGE_COST,
    )
    assert all(
        item.status is LiveEvidenceStatus.FAILED
        and item.failure_code is LiveEvidenceFailureCode.TIMEOUT
        and item.trace_sha256 == "d" * 64
        for item in observations
    )
    assert observations[0].charged_cost_microusd == 4
    assert observations[1].charged_cost_microusd is None


def test_failure_receipt_revision_and_cost_must_match() -> None:
    mismatched = failure_receipt().model_copy(
        update={"adapter_revision_sha256": "f" * 64}
    )
    with pytest.raises(ValueError, match="binding"):
        failed_live_observations(descriptors()[-1], mismatched)
    invalid_cost = failure_receipt().model_dump()
    invalid_cost["charged_cost_microusd"] = 11
    with pytest.raises(ValidationError, match="cost"):
        LiveSmokeFailureReceipt.model_validate(
            invalid_cost
        )
