"""Redacted trace export and deterministic replay tests."""

import hashlib
import json

import pytest

from app.services.harness.evaluation import build_replay_trace, replay_trace


def _state_hash(state: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_trace_redacts_prompt_and_replays_to_matching_state() -> None:
    final_state = {"count": 1}
    trace = build_replay_trace(
        harness_revision_sha256="0" * 64,
        model_revision_sha256="1" * 64,
        configuration_sha256="2" * 64,
        events=(
            (
                "turn.started",
                {"prompt": "secret", "increment": 1},
                _state_hash(final_state),
            ),
        ),
    )
    assert "secret" not in trace.events[0].payload_json
    result = replay_trace(
        trace,
        initial_state={"count": 0},
        reducer=lambda state, payload: {"count": state["count"] + payload["increment"]},
    )
    assert result.passed
    assert result.divergence is None


def test_replay_reports_first_divergence_and_tampering_fails() -> None:
    trace = build_replay_trace(
        harness_revision_sha256="0" * 64,
        model_revision_sha256="1" * 64,
        configuration_sha256="2" * 64,
        events=(("turn.started", {"increment": 1}, "3" * 64),),
    )
    result = replay_trace(
        trace,
        initial_state={"count": 0},
        reducer=lambda state, payload: {"count": state["count"] + payload["increment"]},
    )
    assert not result.passed
    assert result.divergence is not None
    tampered = trace.model_dump(mode="json")
    tampered["trace_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="trace hash"):
        type(trace).model_validate_json(json.dumps(tampered))
