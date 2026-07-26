import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.services.harness.protocol import (
    ExecutionBudget,
    ProviderStreamEvent,
    ProviderToolCall,
)
from app.services.harness.providers import RecordedProviderStream
from app.services.harness.sessions import (
    SingleTurnEngine,
    SingleTurnOutcome,
    ToolResolution,
    ToolResolutionStatus,
    TurnBudgetSnapshot,
    TurnEnginePhase,
    TurnLoopLimits,
    TurnTerminalTrace,
    TurnToolExchangeTrace,
    initial_turn_budget,
)

TURN_ID = "trn_" + "4" * 32
FIXTURE_ROOT = (
    Path(__file__).parent / "fixtures" / "harness"
)
PROVIDER_FIXTURES = FIXTURE_ROOT / "provider_stream"
GOLDEN_PATH = FIXTURE_ROOT / "single_turn" / "golden-sha256.json"


class RecordedAttempts:
    def __init__(self, fixture_names: tuple[str | None, ...]) -> None:
        self._streams = tuple(
            RecordedProviderStream(
                ()
                if fixture_name is None
                else tuple(
                    (PROVIDER_FIXTURES / fixture_name).read_bytes().splitlines()
                )
            )
            for fixture_name in fixture_names
        )

    def open_attempt(
        self,
        attempt: int,
    ) -> AsyncIterator[ProviderStreamEvent]:
        if attempt > len(self._streams):
            return RecordedProviderStream(()).events()
        return self._streams[attempt - 1].events()


class DurableResolver:
    def __init__(self, status: ToolResolutionStatus) -> None:
        self._status = status

    async def resolve(self, call: ProviderToolCall) -> ToolResolution:
        value = json.dumps(
            {
                "call_id": call.call_id,
                "status": self._status.value,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return ToolResolution(
            status=self._status,
            result_sha256=hashlib.sha256(value).hexdigest(),
            result_bytes=len(value),
            reason=(
                "Policy denied the recorded tool call."
                if self._status is ToolResolutionStatus.DENIED
                else None
            ),
        )


def initial_budget(
    *,
    stream_bytes: int = 16 * 1024 * 1024,
) -> TurnBudgetSnapshot:
    return initial_turn_budget(
        TURN_ID,
        ExecutionBudget(
            max_steps=8,
            max_tool_calls=4,
            max_input_tokens=1_000,
            max_output_tokens=1_000,
            max_tool_output_bytes=16 * 1024,
            max_duration_ms=10_000,
            max_cost_microusd=1_000_000,
        ),
        TurnLoopLimits(
            max_provider_retries=2,
            max_identical_calls=2,
            max_stream_bytes=stream_bytes,
        ),
    )


def scenario(
    name: str,
) -> tuple[
    tuple[str | None, ...],
    ToolResolutionStatus,
    int,
    TurnEnginePhase,
    str,
]:
    scenarios = {
        "happy": (
            ("happy.jsonl",),
            ToolResolutionStatus.ALLOWED,
            16 * 1024 * 1024,
            TurnEnginePhase.COMPLETED,
            "completed",
        ),
        "usage": (
            ("happy.jsonl",),
            ToolResolutionStatus.ALLOWED,
            16 * 1024 * 1024,
            TurnEnginePhase.COMPLETED,
            "completed",
        ),
        "error": (
            ("error.jsonl", None),
            ToolResolutionStatus.ALLOWED,
            16 * 1024 * 1024,
            TurnEnginePhase.FAILED,
            "provider_stream_empty",
        ),
        "empty": (
            (None,),
            ToolResolutionStatus.ALLOWED,
            16 * 1024 * 1024,
            TurnEnginePhase.FAILED,
            "provider_stream_empty",
        ),
        "malformed": (
            ("malformed.jsonl",),
            ToolResolutionStatus.ALLOWED,
            16 * 1024 * 1024,
            TurnEnginePhase.FAILED,
            "provider_stream_failed",
        ),
        "retry": (
            ("error.jsonl", "happy.jsonl"),
            ToolResolutionStatus.ALLOWED,
            16 * 1024 * 1024,
            TurnEnginePhase.COMPLETED,
            "completed",
        ),
        "tool": (
            ("tool.jsonl", "happy.jsonl"),
            ToolResolutionStatus.ALLOWED,
            16 * 1024 * 1024,
            TurnEnginePhase.COMPLETED,
            "completed",
        ),
        "denial": (
            ("tool.jsonl", "happy.jsonl"),
            ToolResolutionStatus.DENIED,
            16 * 1024 * 1024,
            TurnEnginePhase.COMPLETED,
            "completed",
        ),
        "cancelled": (
            ("cancelled.jsonl",),
            ToolResolutionStatus.ALLOWED,
            16 * 1024 * 1024,
            TurnEnginePhase.CANCELLED,
            "provider_cancelled",
        ),
        "budget": (
            ("happy.jsonl",),
            ToolResolutionStatus.ALLOWED,
            1,
            TurnEnginePhase.FAILED,
            "budget_exhausted:stream_bytes",
        ),
    }
    return scenarios[name]


async def execute(name: str) -> SingleTurnOutcome:
    fixture_names, status, stream_bytes, _phase, _reason = scenario(name)
    engine = SingleTurnEngine(
        RecordedAttempts(fixture_names),
        DurableResolver(status),
        monotonic_clock=lambda: 0.0,
    )
    return await engine.run(initial_budget(stream_bytes=stream_bytes))


@pytest.mark.parametrize(
    "name",
    (
        "happy",
        "usage",
        "error",
        "empty",
        "malformed",
        "retry",
        "tool",
        "denial",
        "cancelled",
        "budget",
    ),
)
def test_recorded_single_turn_scenarios_match_exact_golden(
    name: str,
) -> None:
    first = asyncio.run(execute(name))
    second = asyncio.run(execute(name))
    serialized = first.model_dump_json().encode()
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    _fixture_names, _status, _stream_bytes, phase, reason = scenario(name)
    terminal = first.events[-1]

    assert isinstance(terminal, TurnTerminalTrace)
    assert first == second
    assert first.phase is phase
    assert terminal.reason == reason
    assert terminal.budget == first.budget
    assert hashlib.sha256(serialized).hexdigest() == golden[name]
    if name in {"happy", "usage"}:
        assert first.budget.usage.input_tokens == 10
        assert first.budget.usage.output_tokens == 4
        assert first.budget.usage.cost_microusd == 12
    if name in {"tool", "denial"}:
        exchanges = tuple(
            event
            for event in first.events
            if isinstance(event, TurnToolExchangeTrace)
        )
        assert len(exchanges) == 1
        assert exchanges[0].resolution.durable
