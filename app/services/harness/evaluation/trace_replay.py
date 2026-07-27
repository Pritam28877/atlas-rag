"""Redacted deterministic trace export and first-divergence replay."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol import Sha256, StrictProtocolModel
from app.services.harness.protocol.base import BoundedLabel, BoundedReason

MAXIMUM_REPLAY_EVENTS = 10_000
MAXIMUM_REPLAY_PAYLOAD_BYTES = 16 * 1024
MAXIMUM_REPLAY_DEPTH = 8
MAXIMUM_REPLAY_ENTRIES = 128
_SENSITIVE_KEYS = (
    "prompt",
    "content",
    "token",
    "secret",
    "password",
    "cookie",
    "authorization",
    "credential",
    "file",
)


class ReplayTraceEvent(StrictProtocolModel):
    sequence: int = Field(ge=1, le=MAXIMUM_REPLAY_EVENTS)
    event_type: BoundedLabel
    payload_json: str = Field(min_length=2, max_length=MAXIMUM_REPLAY_PAYLOAD_BYTES)
    payload_sha256: Sha256
    state_sha256: Sha256

    @model_validator(mode="after")
    def validate_payload_hash(self) -> Self:
        actual_hash = hashlib.sha256(self.payload_json.encode()).hexdigest()
        if actual_hash != self.payload_sha256:
            raise ValueError("replay payload hash is invalid")
        try:
            decoded = json.loads(self.payload_json)
        except json.JSONDecodeError as error:
            raise ValueError("replay payload is not JSON") from error
        if not isinstance(decoded, dict):
            raise ValueError("replay payload must be an object")
        return self


class ReplayTrace(StrictProtocolModel):
    harness_revision_sha256: Sha256
    model_revision_sha256: Sha256
    configuration_sha256: Sha256
    events: tuple[ReplayTraceEvent, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_REPLAY_EVENTS,
    )
    trace_sha256: Sha256

    @model_validator(mode="after")
    def validate_trace(self) -> Self:
        sequences = tuple(event.sequence for event in self.events)
        if sequences != tuple(range(1, len(self.events) + 1)):
            raise ValueError("replay sequences must be contiguous")
        if replay_trace_sha256(self.model_dump(mode="json")) != self.trace_sha256:
            raise ValueError("replay trace hash is invalid")
        return self


class ReplayDivergence(StrictProtocolModel):
    sequence: int = Field(ge=1, le=MAXIMUM_REPLAY_EVENTS)
    expected_state_sha256: Sha256
    actual_state_sha256: Sha256
    reason: BoundedReason


class ReplayResult(StrictProtocolModel):
    passed: bool
    replayed_events: int = Field(ge=0, le=MAXIMUM_REPLAY_EVENTS)
    final_state_sha256: Sha256
    divergence: ReplayDivergence | None = None


ReplayReducer = Callable[
    [Mapping[str, object], Mapping[str, object]], Mapping[str, object]
]


def build_replay_trace(
    *,
    harness_revision_sha256: Sha256,
    model_revision_sha256: Sha256,
    configuration_sha256: Sha256,
    events: Sequence[tuple[str, Mapping[str, object], Sha256]],
) -> ReplayTrace:
    if not 1 <= len(events) <= MAXIMUM_REPLAY_EVENTS:
        raise ValueError("replay event count exceeds the configured bound")
    records: list[ReplayTraceEvent] = []
    for sequence, (event_type, payload, state_sha256) in enumerate(events, start=1):
        redacted_json = _canonical_json(_redact(payload, 0))
        payload_sha256 = hashlib.sha256(redacted_json.encode()).hexdigest()
        records.append(
            ReplayTraceEvent(
                sequence=sequence,
                event_type=event_type,
                payload_json=redacted_json,
                payload_sha256=payload_sha256,
                state_sha256=state_sha256,
            )
        )
    event_records = tuple(records)
    provisional = ReplayTrace.model_construct(
        harness_revision_sha256=harness_revision_sha256,
        model_revision_sha256=model_revision_sha256,
        configuration_sha256=configuration_sha256,
        events=event_records,
        trace_sha256="0" * 64,
    )
    return ReplayTrace(
        harness_revision_sha256=harness_revision_sha256,
        model_revision_sha256=model_revision_sha256,
        configuration_sha256=configuration_sha256,
        events=event_records,
        trace_sha256=replay_trace_sha256(
            provisional.model_dump(mode="json", warnings=False)
        ),
    )


def replay_trace(
    trace: ReplayTrace,
    *,
    initial_state: Mapping[str, object],
    reducer: ReplayReducer,
) -> ReplayResult:
    state: Mapping[str, object] = initial_state
    for event in trace.events:
        payload = json.loads(event.payload_json)
        if not isinstance(payload, dict):
            raise ValueError("replay payload must remain an object")
        state = reducer(state, payload)
        actual_hash = _state_sha256(state)
        if actual_hash != event.state_sha256:
            return ReplayResult(
                passed=False,
                replayed_events=event.sequence,
                final_state_sha256=actual_hash,
                divergence=ReplayDivergence(
                    sequence=event.sequence,
                    expected_state_sha256=event.state_sha256,
                    actual_state_sha256=actual_hash,
                    reason="state diverged during deterministic replay",
                ),
            )
    return ReplayResult(
        passed=True,
        replayed_events=len(trace.events),
        final_state_sha256=_state_sha256(state),
    )


def replay_trace_sha256(values: object) -> str:
    if isinstance(values, dict):
        values = {key: value for key, value in values.items() if key != "trace_sha256"}
    return hashlib.sha256(_canonical_json(values).encode()).hexdigest()


def _state_sha256(state: Mapping[str, object]) -> Sha256:
    return hashlib.sha256(_canonical_json(state).encode()).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _redact(value: object, depth: int) -> object:
    if depth > MAXIMUM_REPLAY_DEPTH:
        return "<truncated>"
    if isinstance(value, str):
        if len(value) > MAXIMUM_REPLAY_PAYLOAD_BYTES:
            marker = "<truncated>"
            return value[: MAXIMUM_REPLAY_PAYLOAD_BYTES - len(marker)] + marker
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "<truncated>"
    if isinstance(value, Mapping):
        bounded: dict[str, object] = {}
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= MAXIMUM_REPLAY_ENTRIES:
                break
            key = str(raw_key)
            if any(part in key.casefold() for part in _SENSITIVE_KEYS):
                bounded[key] = "<redacted>"
            else:
                bounded[key] = _redact(raw_value, depth + 1)
        return bounded
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_redact(item, depth + 1) for item in value[:MAXIMUM_REPLAY_ENTRIES]]
    return "<truncated>"
