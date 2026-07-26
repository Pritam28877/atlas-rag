import asyncio
import hashlib
from datetime import timedelta

import pytest

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderCompleted,
    ProviderFinishReason,
    ProviderReasoningDelta,
    ProviderTextDelta,
    ProviderTokenUsage,
    ProviderToolCall,
    ProviderUsage,
)
from app.services.harness.providers.local_compatible_capabilities import (
    LocalCompatibleFeature,
)
from app.services.harness.providers.local_compatible_probe import (
    BoundedLocalCompatibleProbeRunner,
    LocalProbeCase,
    LocalProbeCaseResult,
    LocalProbeRunnerError,
    LocalProbeRunnerErrorCode,
)
from tests.harness.providers.local_compatible.fixtures import (
    NOW,
    authorized_route,
    local_model,
)


class FakeProbeBackend:
    def __init__(
        self,
        *,
        missing_usage: bool = False,
        missing_cancellation: bool = False,
        fail_every_case: bool = False,
    ) -> None:
        self.missing_usage = missing_usage
        self.missing_cancellation = missing_cancellation
        self.fail_every_case = fail_every_case
        self.cases: list[LocalProbeCase] = []

    async def run_case(
        self,
        case: LocalProbeCase,
        *,
        cancellation: asyncio.Event,
        cancel_after_first_event: bool,
        deadline_at,
    ) -> LocalProbeCaseResult:
        del cancellation, deadline_at
        self.cases.append(case)
        if self.fail_every_case or (
            case is LocalProbeCase.CANCELLATION
            and self.missing_cancellation
        ):
            return _result(case, ())
        if case is LocalProbeCase.BASE_STREAM:
            events = (
                ProviderTextDelta(sequence=1, text="probe secret"),
                *(() if self.missing_usage else (_usage(2),)),
                ProviderCompleted(
                    sequence=2 if self.missing_usage else 3,
                    finish_reason=ProviderFinishReason.STOP,
                ),
            )
        elif case is LocalProbeCase.CANCELLATION:
            assert cancel_after_first_event
            events = (
                ProviderCancelled(
                    sequence=1,
                    reason="Probe transport cancellation.",
                ),
            )
        elif case is LocalProbeCase.DEVELOPER_ROLE:
            events = (
                ProviderTextDelta(sequence=1, text="developer accepted"),
                ProviderCompleted(
                    sequence=2,
                    finish_reason=ProviderFinishReason.STOP,
                ),
            )
        elif case is LocalProbeCase.PROMPT_CACHE:
            events = (
                _usage(1, cached_tokens=1),
                ProviderCompleted(
                    sequence=2,
                    finish_reason=ProviderFinishReason.STOP,
                ),
            )
        elif case is LocalProbeCase.REASONING:
            events = (
                ProviderReasoningDelta(sequence=1, text="reasoning"),
                _usage(2, reasoning_tokens=1),
                ProviderCompleted(
                    sequence=3,
                    finish_reason=ProviderFinishReason.STOP,
                ),
            )
        else:
            events = (
                _tool_call(1, "first"),
                _tool_call(2, "second"),
                _usage(3),
                ProviderCompleted(
                    sequence=4,
                    finish_reason=ProviderFinishReason.TOOL_CALLS,
                ),
            )
        return _result(case, events)


class HangingProbeBackend:
    async def run_case(self, *args, **kwargs):
        del args, kwargs
        await asyncio.Event().wait()


def test_probe_derives_full_features_without_retaining_text() -> None:
    backend = FakeProbeBackend()
    runner = BoundedLocalCompatibleProbeRunner(
        backend,
        clock=lambda: NOW,
    )

    probe = asyncio.run(
        runner.probe(
            authorized_route(),
            model_revision_sha256=local_model().model_revision_sha256,
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=30),
        )
    )

    assert LocalCompatibleFeature.STREAM_USAGE in probe.supported_features
    assert LocalCompatibleFeature.STREAM_CANCEL in probe.supported_features
    assert LocalCompatibleFeature.TOOLS_PARALLEL in probe.supported_features
    assert LocalCompatibleFeature.REASONING_STREAM in probe.supported_features
    assert tuple(backend.cases) == tuple(LocalProbeCase)
    assert "probe secret" not in probe.model_dump_json()


def test_missing_usage_and_cancellation_are_not_claimed() -> None:
    runner = BoundedLocalCompatibleProbeRunner(
        FakeProbeBackend(
            missing_usage=True,
            missing_cancellation=True,
        ),
        clock=lambda: NOW,
    )

    probe = asyncio.run(
        runner.probe(
            authorized_route(),
            model_revision_sha256=local_model().model_revision_sha256,
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=30),
        )
    )

    assert LocalCompatibleFeature.STREAM_USAGE not in probe.supported_features
    assert LocalCompatibleFeature.STREAM_CANCEL not in probe.supported_features


def test_fully_incompatible_endpoint_produces_empty_evidence() -> None:
    runner = BoundedLocalCompatibleProbeRunner(
        FakeProbeBackend(fail_every_case=True),
        clock=lambda: NOW,
    )

    probe = asyncio.run(
        runner.probe(
            authorized_route(),
            model_revision_sha256=local_model().model_revision_sha256,
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=30),
        )
    )

    assert probe.supported_features == ()


def test_probe_honors_external_cancellation_and_case_timeout() -> None:
    cancelled = asyncio.Event()
    cancelled.set()
    runner = BoundedLocalCompatibleProbeRunner(
        FakeProbeBackend(),
        clock=lambda: NOW,
    )
    with pytest.raises(LocalProbeRunnerError) as cancellation_error:
        asyncio.run(
            runner.probe(
                authorized_route(),
                model_revision_sha256=local_model().model_revision_sha256,
                cancellation=cancelled,
                deadline_at=NOW + timedelta(seconds=30),
            )
        )
    assert (
        cancellation_error.value.code
        is LocalProbeRunnerErrorCode.CANCELLED
    )

    timeout_runner = BoundedLocalCompatibleProbeRunner(
        HangingProbeBackend(),
        clock=lambda: NOW,
        per_case_timeout_seconds=0.1,
    )
    with pytest.raises(LocalProbeRunnerError) as timeout_error:
        asyncio.run(
            timeout_runner.probe(
                authorized_route(),
                model_revision_sha256=local_model().model_revision_sha256,
                cancellation=asyncio.Event(),
                deadline_at=NOW + timedelta(seconds=30),
            )
        )
    assert timeout_error.value.code is LocalProbeRunnerErrorCode.TIMEOUT


def _result(
    case: LocalProbeCase,
    events,
) -> LocalProbeCaseResult:
    passed = bool(events)
    return LocalProbeCaseResult(
        case=case,
        request_sha256=hashlib.sha256(case.value.encode()).hexdigest(),
        response_bytes=100 if passed else 0,
        events=events,
        transport_cancelled=(
            passed and case is LocalProbeCase.CANCELLATION
        ),
        passed=passed,
    )


def _usage(
    sequence: int,
    *,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> ProviderUsage:
    return ProviderUsage(
        sequence=sequence,
        usage=ProviderTokenUsage(
            input_tokens=2,
            cached_input_tokens=cached_tokens,
            output_tokens=1,
            reasoning_tokens=reasoning_tokens,
            cost_microusd=0,
        ),
    )


def _tool_call(sequence: int, name: str) -> ProviderToolCall:
    arguments = "{}"
    return ProviderToolCall(
        sequence=sequence,
        call_id=f"probe_{name}",
        tool_name=name,
        arguments_json=arguments,
        arguments_sha256=hashlib.sha256(arguments.encode()).hexdigest(),
    )
