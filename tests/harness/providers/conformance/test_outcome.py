import hashlib

from app.services.harness.protocol import (
    ProviderCompleted,
    ProviderFinishReason,
    ProviderTextDelta,
    ProviderTokenUsage,
    ProviderUsage,
)
from app.services.harness.providers.conformance_outcome import (
    ConformanceOutcomeAccumulator,
)


def test_projection_ignores_text_chunking_and_usage_values() -> None:
    chunked = ConformanceOutcomeAccumulator()
    chunked.consume(ProviderTextDelta(sequence=1, text="same "))
    chunked.consume(ProviderTextDelta(sequence=2, text="answer"))
    chunked.consume(_usage(sequence=3, input_tokens=2))
    chunked.consume(
        ProviderCompleted(
            sequence=4,
            finish_reason=ProviderFinishReason.STOP,
        )
    )

    combined = ConformanceOutcomeAccumulator()
    combined.consume(
        ProviderTextDelta(sequence=1, text="same answer")
    )
    combined.consume(_usage(sequence=2, input_tokens=20))
    combined.consume(
        ProviderCompleted(
            sequence=3,
            finish_reason=ProviderFinishReason.STOP,
        )
    )

    first = chunked.finish()
    second = combined.finish()
    assert first.equivalence_sha256 == second.equivalence_sha256
    assert first.usage != second.usage
    assert first.text_sha256 == hashlib.sha256(
        b"same answer"
    ).hexdigest()


def test_projection_preserves_semantic_text_and_terminal_differences() -> None:
    first = _completed_text("first", ProviderFinishReason.STOP)
    second = _completed_text("second", ProviderFinishReason.STOP)
    limited = _completed_text("first", ProviderFinishReason.LENGTH)

    assert first.equivalence_sha256 != second.equivalence_sha256
    assert first.equivalence_sha256 != limited.equivalence_sha256


def _completed_text(
    text: str,
    finish_reason: ProviderFinishReason,
):
    accumulator = ConformanceOutcomeAccumulator()
    accumulator.consume(ProviderTextDelta(sequence=1, text=text))
    accumulator.consume(
        ProviderCompleted(
            sequence=2,
            finish_reason=finish_reason,
        )
    )
    return accumulator.finish()


def _usage(sequence: int, input_tokens: int) -> ProviderUsage:
    return ProviderUsage(
        sequence=sequence,
        usage=ProviderTokenUsage(
            input_tokens=input_tokens,
            cached_input_tokens=0,
            output_tokens=1,
            reasoning_tokens=0,
            cost_microusd=input_tokens + 1,
        ),
    )
