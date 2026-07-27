"""Grounded-completion gate for evidence-ledger snapshots."""

from __future__ import annotations

from pydantic import Field

from app.services.harness.documents.contracts import (
    EvidenceClassification,
    EvidenceFinishOutcome,
    EvidenceLedgerSnapshot,
)
from app.services.harness.protocol.base import BoundedReason, StrictProtocolModel
from app.services.harness.protocol.conversation import SourceId


class GroundedFinishDecision(StrictProtocolModel):
    outcome: EvidenceFinishOutcome
    accepted_claim_ids: tuple[SourceId, ...] = Field(max_length=256)
    blocked_claim_ids: tuple[SourceId, ...] = Field(max_length=256)
    reason: BoundedReason


def validate_grounded_finish(
    snapshot: EvidenceLedgerSnapshot,
) -> GroundedFinishDecision:
    candidate_by_id = {
        candidate.candidate_id: candidate for candidate in snapshot.candidates
    }
    classification_by_id = {
        classification.candidate_id: classification
        for classification in snapshot.classifications
    }
    citation_by_id = {citation.citation_id: citation for citation in snapshot.citations}
    accepted: list[SourceId] = []
    blocked: list[SourceId] = []
    for claim in snapshot.claims:
        claim_supported = bool(claim.citation_ids)
        for citation_id in claim.citation_ids:
            citation = citation_by_id.get(citation_id)
            candidate = (
                None
                if citation is None
                else candidate_by_id.get(citation.candidate_id)
            )
            classification = (
                None
                if citation is None
                else classification_by_id.get(citation.candidate_id)
            )
            if (
                citation is None
                or candidate is None
                or classification is None
                or classification.classification is not EvidenceClassification.RELEVANT
                or citation.query_id != candidate.query_id
                or citation.span != candidate.span
            ):
                claim_supported = False
                break
        if claim_supported:
            accepted.append(claim.claim_id)
        else:
            blocked.append(claim.claim_id)
    accepted_tuple = tuple(sorted(accepted))
    blocked_tuple = tuple(sorted(blocked))
    if blocked_tuple:
        return GroundedFinishDecision(
            outcome=EvidenceFinishOutcome.BLOCKED,
            accepted_claim_ids=accepted_tuple,
            blocked_claim_ids=blocked_tuple,
            reason="every claim requires relevant immutable citation evidence",
        )
    if not accepted_tuple:
        return GroundedFinishDecision(
            outcome=EvidenceFinishOutcome.UNSUPPORTED,
            accepted_claim_ids=(),
            blocked_claim_ids=(),
            reason="no grounded claims were recorded",
        )
    return GroundedFinishDecision(
        outcome=EvidenceFinishOutcome.GROUNDED,
        accepted_claim_ids=accepted_tuple,
        blocked_claim_ids=(),
        reason="all claims have relevant immutable citations",
    )
