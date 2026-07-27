"""Release decision tests."""

import pytest
from pydantic import ValidationError

from app.services.harness.evaluation.release import (
    ReleaseBlocker,
    ReleaseDecision,
    ReleaseStatus,
    build_release_decision,
)


def _blocker() -> ReleaseBlocker:
    return ReleaseBlocker(
        code="live_provider_evidence",
        summary="Disposable provider smokes are not recorded",
        owner="provider-on-call",
        evidence_ref="docs/harness/09-cross-provider-live-matrix.md",
    )


def test_release_decision_stays_blocked_for_open_gate() -> None:
    decision = build_release_decision((_blocker(),))
    assert decision.status is ReleaseStatus.BLOCKED
    assert decision.blockers == (_blocker(),)


def test_release_decision_requires_human_approval_and_digest() -> None:
    with pytest.raises(ValidationError, match="artifact digest"):
        build_release_decision((), human_approved=True)

    decision = build_release_decision((), artifact_sha256="a" * 64)
    assert decision.status is ReleaseStatus.BLOCKED

    with pytest.raises(ValidationError, match="approved release"):
        ReleaseDecision(
            status=ReleaseStatus.APPROVED,
            blockers=(),
            human_approved=False,
            artifact_sha256="a" * 64,
        )


def test_blocked_decision_cannot_hide_empty_blocker_list() -> None:
    with pytest.raises(ValidationError, match="at least one blocker"):
        ReleaseDecision(
            status=ReleaseStatus.BLOCKED,
            blockers=(),
            human_approved=False,
        )
