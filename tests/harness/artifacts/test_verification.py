"""Independent verifier, bounded repair, and staged conflict tests."""

import pytest

from app.services.harness.artifacts import (
    HandoffChange,
    HandoffChangeKind,
    InMemoryPatchConflictResolver,
    PatchConflictState,
    PatchProposal,
    RepairRequest,
    RepairState,
    VerificationAcceptance,
    VerifierLedger,
    VerifierRun,
    VerifierStatus,
    patch_proposal_sha256,
    repair_request_sha256,
    verifier_run_sha256,
)
from app.services.harness.protocol import ExecutionBudget
from tests.harness.artifacts.test_handoffs import (
    ARTIFACT,
    NOW,
    PRODUCER,
    VERIFIER,
    WORKSPACE,
)


def _run(gate: str, status: VerifierStatus) -> VerifierRun:
    values = {
        "handoff_id": "art_" + "8" * 32,
        "producer_task_id": PRODUCER,
        "verifier_task_id": VERIFIER,
        "gate": gate,
        "status": status,
        "evidence_artifact_ids": (ARTIFACT,) if status is VerifierStatus.PASSED else (),
        "reason": None if status is VerifierStatus.PASSED else "gate failed",
        "observed_at": NOW,
    }
    provisional = VerifierRun.model_construct(**values)
    return VerifierRun(
        **values,
        run_sha256=verifier_run_sha256(
            provisional.model_dump(mode="json", warnings=False)
        ),
    )


def test_independent_verifier_acceptance_and_repair_bound() -> None:
    ledger = VerifierLedger()
    ledger.record_run(_run("tests", VerifierStatus.PASSED))
    accepted = ledger.accept("art_" + "8" * 32, ("tests",))
    assert isinstance(accepted, VerificationAcceptance)
    assert accepted.accepted

    values = {
        "handoff_id": "art_" + "8" * 32,
        "producer_task_id": PRODUCER,
        "verifier_task_id": VERIFIER,
        "attempt": 1,
        "maximum_attempts": 2,
        "budget": ExecutionBudget(
            max_steps=2,
            max_tool_calls=1,
            max_input_tokens=10,
            max_output_tokens=10,
            max_tool_output_bytes=10,
            max_duration_ms=200,
            max_cost_microusd=2,
        ),
        "reason": "repair failed tests",
        "requested_at": NOW,
    }
    repair = RepairRequest(
        **values,
        repair_id=repair_request_sha256(
            RepairRequest.model_construct(**values, repair_id="art_" + "0" * 32)
        ),
    )
    requested = ledger.request_repair(repair)
    completed = ledger.complete_repair(repair.repair_id, ARTIFACT)
    assert requested.state is RepairState.REQUESTED
    assert completed.state is RepairState.COMPLETED


def test_conflicting_shared_changes_are_staged_until_independent_resolution() -> None:
    def proposal(task_suffix: str, proposal_suffix: str) -> PatchProposal:
        values = {
            "proposal_id": "art_" + proposal_suffix * 32,
            "producer_task_id": "tsk_" + task_suffix * 32,
            "workspace_id": WORKSPACE,
            "changes": (
                HandoffChange(
                    path="src/app.py",
                    kind=HandoffChangeKind.MODIFIED,
                    before_sha256="a" * 64,
                    after_sha256=proposal_suffix * 64,
                ),
            ),
        }
        provisional = PatchProposal.model_construct(**values)
        return PatchProposal(
            **values,
            proposal_sha256=patch_proposal_sha256(
                provisional.model_dump(mode="json", warnings=False)
            ),
        )

    first = proposal("3", "1")
    second = proposal("4", "2")
    resolver = InMemoryPatchConflictResolver()
    conflicts = resolver.stage((first, second))
    assert len(conflicts) == 1
    assert conflicts[0].state is PatchConflictState.STAGED
    with pytest.raises(ValueError, match="independent"):
        resolver.resolve(
            conflicts[0].conflict_id,
            first.proposal_id,
            first.producer_task_id,
        )
    resolved = resolver.resolve(conflicts[0].conflict_id, first.proposal_id, VERIFIER)
    assert resolved.state is PatchConflictState.RESOLVED
    assert resolved.resolved_proposal_id == first.proposal_id
