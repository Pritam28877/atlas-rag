"""Bounded behavioral probe for local OpenAI-compatible endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderCompleted,
    ProviderError,
    ProviderStreamEvent,
    Sha256,
    StrictProtocolModel,
)
from app.services.harness.providers.local_compatible_capabilities import (
    LocalCompatibleProbe,
)
from app.services.harness.providers.local_compatible_policy import (
    AuthorizedLocalCompatibleRoute,
)
from app.services.harness.providers.local_compatible_probe_analysis import (
    evidence_sha256,
    observed_features,
)

MAXIMUM_LOCAL_PROBE_EVENTS = 256
MAXIMUM_LOCAL_PROBE_RESPONSE_BYTES = 1024 * 1024


class LocalProbeCase(StrEnum):
    BASE_STREAM = "base_stream"
    CANCELLATION = "cancellation"
    DEVELOPER_ROLE = "developer_role"
    PROMPT_CACHE = "prompt_cache"
    REASONING = "reasoning"
    TOOLS = "tools"


PROBE_CASES = (
    LocalProbeCase.BASE_STREAM,
    LocalProbeCase.CANCELLATION,
    LocalProbeCase.DEVELOPER_ROLE,
    LocalProbeCase.PROMPT_CACHE,
    LocalProbeCase.REASONING,
    LocalProbeCase.TOOLS,
)


class LocalProbeCaseResult(StrictProtocolModel):
    case: LocalProbeCase
    request_sha256: Sha256
    response_bytes: int = Field(ge=0, le=MAXIMUM_LOCAL_PROBE_RESPONSE_BYTES)
    events: tuple[ProviderStreamEvent, ...] = Field(
        max_length=MAXIMUM_LOCAL_PROBE_EVENTS
    )
    transport_cancelled: bool
    passed: bool

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.passed != bool(self.events):
            raise ValueError("passed local probe cases require events")
        if not self.events:
            if self.response_bytes or self.transport_cancelled:
                raise ValueError("failed local probe case contains outcome data")
            return self
        sequences = tuple(event.sequence for event in self.events)
        first = sequences[0]
        if sequences != tuple(range(first, first + len(sequences))):
            raise ValueError("local probe event sequence is not contiguous")
        if not isinstance(
            self.events[-1],
            (ProviderCancelled, ProviderCompleted, ProviderError),
        ):
            raise ValueError("local probe result is not terminal")
        if self.transport_cancelled != (
            self.case is LocalProbeCase.CANCELLATION
        ):
            raise ValueError("local probe cancellation evidence is inconsistent")
        return self


class LocalProbeBackend(Protocol):
    async def run_case(
        self,
        case: LocalProbeCase,
        *,
        cancellation: asyncio.Event,
        cancel_after_first_event: bool,
        deadline_at: datetime,
    ) -> LocalProbeCaseResult: ...


class LocalProbeRunnerErrorCode(StrEnum):
    CANCELLED = "cancelled"
    CLOCK = "clock"
    DEADLINE = "deadline"
    TIMEOUT = "timeout"


class LocalProbeRunnerError(RuntimeError):
    def __init__(self, code: LocalProbeRunnerErrorCode) -> None:
        super().__init__("local-compatible probe execution failed")
        self.code = code


class BoundedLocalCompatibleProbeRunner:
    def __init__(
        self,
        backend: LocalProbeBackend,
        *,
        clock: Callable[[], datetime],
        per_case_timeout_seconds: float = 5.0,
        evidence_ttl_seconds: int = 900,
    ) -> None:
        if not 0.1 <= per_case_timeout_seconds <= 30:
            raise ValueError("local probe case timeout is invalid")
        if not 1 <= evidence_ttl_seconds <= 3_600:
            raise ValueError("local probe evidence TTL is invalid")
        self._backend = backend
        self._clock = clock
        self._case_timeout = per_case_timeout_seconds
        self._evidence_ttl = evidence_ttl_seconds

    async def probe(
        self,
        route: AuthorizedLocalCompatibleRoute,
        *,
        model_revision_sha256: Sha256,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> LocalCompatibleProbe:
        observed_at = self._clock()
        _require_utc(observed_at)
        _require_utc(deadline_at)
        if deadline_at <= observed_at:
            raise LocalProbeRunnerError(LocalProbeRunnerErrorCode.DEADLINE)
        results: list[LocalProbeCaseResult] = []
        for case in PROBE_CASES:
            if cancellation.is_set():
                raise LocalProbeRunnerError(
                    LocalProbeRunnerErrorCode.CANCELLED
                )
            now = self._clock()
            _require_utc(now)
            remaining = (deadline_at - now).total_seconds()
            if remaining <= 0:
                raise LocalProbeRunnerError(
                    LocalProbeRunnerErrorCode.DEADLINE
                )
            timeout_seconds = min(self._case_timeout, remaining)
            case_cancellation = asyncio.Event()
            try:
                async with asyncio.timeout(timeout_seconds):
                    result = await self._backend.run_case(
                        case,
                        cancellation=case_cancellation,
                        cancel_after_first_event=(
                            case is LocalProbeCase.CANCELLATION
                        ),
                        deadline_at=deadline_at,
                    )
            except TimeoutError:
                case_cancellation.set()
                raise LocalProbeRunnerError(
                    LocalProbeRunnerErrorCode.TIMEOUT
                ) from None
            if result.case is not case:
                raise ValueError("local probe backend returned another case")
            results.append(result)
        features = observed_features(tuple(results))
        evidence_digest = evidence_sha256(tuple(results), features)
        return LocalCompatibleProbe(
            route_id=route.route_id,
            model_id=route.model_id,
            model_revision_sha256=model_revision_sha256,
            destination_sha256=route.destination_sha256,
            supported_features=features,
            evidence_sha256=evidence_digest,
            observed_at=observed_at,
            expires_at=observed_at
            + timedelta(seconds=self._evidence_ttl),
        )

def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise LocalProbeRunnerError(LocalProbeRunnerErrorCode.CLOCK)
