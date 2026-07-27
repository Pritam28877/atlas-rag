"""Explicit release decisions that cannot hide open acceptance gates."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedLabel,
    BoundedReason,
    Sha256,
    StrictProtocolModel,
)

MAXIMUM_RELEASE_BLOCKERS = 64


class ReleaseStatus(StrEnum):
    BLOCKED = "blocked"
    APPROVED = "approved"


class ReleaseBlocker(StrictProtocolModel):
    code: BoundedLabel
    summary: BoundedReason
    owner: BoundedLabel
    evidence_ref: BoundedLabel


class ReleaseDecision(StrictProtocolModel):
    status: ReleaseStatus
    blockers: tuple[ReleaseBlocker, ...] = Field(
        max_length=MAXIMUM_RELEASE_BLOCKERS
    )
    human_approved: bool
    artifact_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.status is ReleaseStatus.APPROVED:
            if self.blockers:
                raise ValueError("approved release cannot have blockers")
            if not self.human_approved:
                raise ValueError("approved release requires human approval")
            if self.artifact_sha256 is None:
                raise ValueError("approved release requires artifact digest")
        elif not self.blockers:
            raise ValueError("blocked release requires at least one blocker")
        return self


def build_release_decision(
    blockers: tuple[ReleaseBlocker, ...],
    *,
    human_approved: bool = False,
    artifact_sha256: Sha256 | None = None,
) -> ReleaseDecision:
    """Build a fail-closed decision from explicit evidence blockers."""

    effective_blockers = blockers
    if not blockers and not human_approved:
        effective_blockers = (
            ReleaseBlocker(
                code="human_approval_required",
                summary="A human release approver has not signed the candidate",
                owner="platform-lead",
                evidence_ref="docs/harness/15-release-decision.md",
            ),
        )
    status = ReleaseStatus.APPROVED if not effective_blockers else ReleaseStatus.BLOCKED
    return ReleaseDecision(
        status=status,
        blockers=effective_blockers,
        human_approved=human_approved,
        artifact_sha256=artifact_sha256,
    )
