import pytest

from app.services.harness.sessions import (
    TurnEnginePhase,
    TurnEngineSignal,
    TurnEngineSnapshot,
    TurnEngineTransitionError,
    allowed_turn_signals,
    transition_turn,
)

TURN_ID = "trn_" + "1" * 32


def snapshot(phase: TurnEnginePhase) -> TurnEngineSnapshot:
    if phase is TurnEnginePhase.ACCEPTED:
        return TurnEngineSnapshot(turn_id=TURN_ID)
    return TurnEngineSnapshot(
        turn_id=TURN_ID,
        phase=phase,
        transition_sequence=1,
        last_signal=TurnEngineSignal.START_CONTEXT,
    )


def test_every_phase_signal_pair_is_explicitly_allowed_or_rejected() -> None:
    for phase in TurnEnginePhase:
        allowed = allowed_turn_signals(phase)
        for signal in TurnEngineSignal:
            current = snapshot(phase)
            if signal in allowed:
                previous_sequence = current.transition_sequence
                updated = transition_turn(current, signal)
                assert updated.phase is not current.phase
                assert updated.last_signal is signal
                assert updated.transition_sequence == previous_sequence + 1
                assert current.transition_sequence == previous_sequence
                continue
            with pytest.raises(TurnEngineTransitionError) as captured:
                transition_turn(current, signal)
            assert captured.value.phase is phase
            assert captured.value.signal is signal


def test_happy_tool_and_retry_paths_are_deterministic() -> None:
    current = TurnEngineSnapshot(turn_id=TURN_ID)
    signals = (
        TurnEngineSignal.START_CONTEXT,
        TurnEngineSignal.CONTEXT_READY,
        TurnEngineSignal.TOOL_REQUESTED,
        TurnEngineSignal.RETRY_REQUIRED,
        TurnEngineSignal.RETRY_READY,
        TurnEngineSignal.COMPLETE,
    )
    expected_phases = (
        TurnEnginePhase.CONTEXT,
        TurnEnginePhase.MODEL,
        TurnEnginePhase.TOOL,
        TurnEnginePhase.RETRY,
        TurnEnginePhase.MODEL,
        TurnEnginePhase.COMPLETED,
    )

    for sequence, (signal, expected_phase) in enumerate(
        zip(signals, expected_phases, strict=True),
        start=1,
    ):
        current = transition_turn(current, signal)
        assert current.phase is expected_phase
        assert current.transition_sequence == sequence


@pytest.mark.parametrize(
    "terminal_phase",
    (
        TurnEnginePhase.COMPLETED,
        TurnEnginePhase.FAILED,
        TurnEnginePhase.CANCELLED,
    ),
)
def test_terminal_phases_absorb_every_signal(
    terminal_phase: TurnEnginePhase,
) -> None:
    terminal = snapshot(terminal_phase)

    assert allowed_turn_signals(terminal_phase) == frozenset()
    for signal in TurnEngineSignal:
        with pytest.raises(TurnEngineTransitionError):
            transition_turn(terminal, signal)


def test_initial_snapshot_requires_accepted_phase_and_no_signal() -> None:
    with pytest.raises(ValueError, match="initial engine phase"):
        TurnEngineSnapshot(
            turn_id=TURN_ID,
            phase=TurnEnginePhase.MODEL,
        )
    with pytest.raises(ValueError, match="last signal"):
        TurnEngineSnapshot(
            turn_id=TURN_ID,
            last_signal=TurnEngineSignal.START_CONTEXT,
        )
