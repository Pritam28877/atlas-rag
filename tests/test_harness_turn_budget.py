import pytest

from app.services.harness.protocol import ExecutionBudget
from app.services.harness.sessions import (
    TurnBudgetCharge,
    TurnBudgetDimension,
    TurnBudgetExceeded,
    TurnBudgetSnapshot,
    TurnLoopLimits,
    charge_turn_budget,
    initial_turn_budget,
)

TURN_ID = "trn_" + "2" * 32
CALL_SHA256 = "a" * 64


def budget() -> ExecutionBudget:
    return ExecutionBudget(
        max_steps=3,
        max_tool_calls=2,
        max_input_tokens=20,
        max_output_tokens=10,
        max_tool_output_bytes=100,
        max_duration_ms=500,
        max_cost_microusd=1000,
    )


def limits() -> TurnLoopLimits:
    return TurnLoopLimits(
        max_provider_retries=1,
        max_identical_calls=2,
        max_stream_bytes=50,
    )


def test_serialized_resume_preserves_every_aggregate_counter() -> None:
    initial = initial_turn_budget(TURN_ID, budget(), limits())
    first = charge_turn_budget(
        initial,
        TurnBudgetCharge(
            steps=1,
            tool_calls=1,
            input_tokens=10,
            output_tokens=2,
            tool_output_bytes=20,
            duration_ms=100,
            cost_microusd=200,
            provider_retries=1,
            stream_bytes=20,
            call_sha256=CALL_SHA256,
        ),
    )
    resumed = TurnBudgetSnapshot.model_validate_json(first.model_dump_json())
    second = charge_turn_budget(
        resumed,
        TurnBudgetCharge(
            steps=1,
            tool_calls=1,
            output_tokens=3,
            tool_output_bytes=30,
            duration_ms=100,
            stream_bytes=10,
            call_sha256=CALL_SHA256,
        ),
    )

    assert initial.revision == 0
    assert second.revision == 2
    assert second.usage.steps == 2
    assert second.usage.tool_calls == 2
    assert second.usage.input_tokens == 10
    assert second.usage.output_tokens == 5
    assert second.usage.tool_output_bytes == 50
    assert second.usage.duration_ms == 200
    assert second.usage.cost_microusd == 200
    assert second.provider_retries == 1
    assert second.stream_bytes == 30
    assert second.repeated_calls[0].count == 2


@pytest.mark.parametrize(
    ("first_charge", "excess_charge", "dimension"),
    (
        (
            TurnBudgetCharge(steps=3),
            TurnBudgetCharge(steps=1),
            TurnBudgetDimension.STEPS,
        ),
        (
            TurnBudgetCharge(input_tokens=20),
            TurnBudgetCharge(input_tokens=1),
            TurnBudgetDimension.INPUT_TOKENS,
        ),
        (
            TurnBudgetCharge(output_tokens=10),
            TurnBudgetCharge(output_tokens=1),
            TurnBudgetDimension.OUTPUT_TOKENS,
        ),
        (
            TurnBudgetCharge(duration_ms=500),
            TurnBudgetCharge(duration_ms=1),
            TurnBudgetDimension.DURATION_MS,
        ),
        (
            TurnBudgetCharge(cost_microusd=1000),
            TurnBudgetCharge(cost_microusd=1),
            TurnBudgetDimension.COST_MICROUSD,
        ),
        (
            TurnBudgetCharge(stream_bytes=50),
            TurnBudgetCharge(stream_bytes=1),
            TurnBudgetDimension.STREAM_BYTES,
        ),
        (
            TurnBudgetCharge(provider_retries=1),
            TurnBudgetCharge(provider_retries=1),
            TurnBudgetDimension.PROVIDER_RETRIES,
        ),
    ),
)
def test_resume_cannot_reset_any_scalar_budget(
    first_charge: TurnBudgetCharge,
    excess_charge: TurnBudgetCharge,
    dimension: TurnBudgetDimension,
) -> None:
    charged = charge_turn_budget(
        initial_turn_budget(TURN_ID, budget(), limits()),
        first_charge,
    )
    resumed = TurnBudgetSnapshot.model_validate_json(charged.model_dump_json())

    with pytest.raises(TurnBudgetExceeded) as captured:
        charge_turn_budget(resumed, excess_charge)

    assert dimension in captured.value.dimensions
    assert resumed == charged


def test_repeated_call_limit_is_aggregate_and_failed_charge_is_atomic() -> None:
    current = initial_turn_budget(TURN_ID, budget(), limits())
    for _ in range(2):
        current = charge_turn_budget(
            current,
            TurnBudgetCharge(
                tool_calls=1,
                call_sha256=CALL_SHA256,
            ),
        )
    before_excess = current

    with pytest.raises(TurnBudgetExceeded) as captured:
        charge_turn_budget(
            current,
            TurnBudgetCharge(
                tool_calls=1,
                call_sha256=CALL_SHA256,
            ),
        )

    assert captured.value.dimensions == (
        TurnBudgetDimension.TOOL_CALLS,
        TurnBudgetDimension.IDENTICAL_CALLS,
    )
    assert current == before_excess


def test_charge_requires_real_resource_and_paired_call_digest() -> None:
    with pytest.raises(ValueError, match="consume a resource"):
        TurnBudgetCharge()
    with pytest.raises(ValueError, match="exactly one call digest"):
        TurnBudgetCharge(tool_calls=1)
    with pytest.raises(ValueError, match="exactly one call digest"):
        TurnBudgetCharge(steps=1, call_sha256=CALL_SHA256)
