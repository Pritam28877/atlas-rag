"""Typed lifecycle contracts for trusted hook dispatch."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from app.services.harness.protocol import EventId, StrictProtocolModel
from app.services.harness.protocol.base import BoundedLabel, BoundedReason

HOOK_MAX_PAYLOAD_BYTES = 128 * 1024
HOOK_MAX_REGISTRATIONS = 64


class HookMode(StrEnum):
    SECURITY = "security"
    ADVISORY = "advisory"
    NOTIFICATION = "notification"


class HookFailurePolicy(StrEnum):
    FAIL_CLOSED = "fail_closed"
    CONTINUE = "continue"


class HookLifecycleEvent(StrEnum):
    BEFORE_OPERATION = "before_operation"
    AFTER_OPERATION = "after_operation"
    BEFORE_PROVIDER = "before_provider"
    AFTER_PROVIDER = "after_provider"
    WORKSPACE_CLOSED = "workspace_closed"


class HookOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ABSTAIN = "abstain"


class HookDescriptor(StrictProtocolModel):
    name: BoundedLabel
    version: BoundedLabel
    mode: HookMode
    events: tuple[HookLifecycleEvent, ...] = Field(max_length=8)
    failure_policy: HookFailurePolicy
    timeout_ms: int = Field(default=5_000, ge=100, le=60_000)

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if not self.events or tuple(sorted(set(self.events), key=str)) != self.events:
            raise ValueError("hook events must be unique and sorted")
        expected = (
            HookFailurePolicy.FAIL_CLOSED
            if self.mode is HookMode.SECURITY
            else HookFailurePolicy.CONTINUE
        )
        if self.failure_policy is not expected:
            raise ValueError("hook failure policy does not match hook mode")
        return self


class HookEvent(StrictProtocolModel):
    event_id: EventId
    event_type: HookLifecycleEvent
    payload_json: str = Field(min_length=2, max_length=HOOK_MAX_PAYLOAD_BYTES)

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if len(self.payload_json.encode("utf-8")) > HOOK_MAX_PAYLOAD_BYTES:
            raise ValueError("hook payload exceeds byte limit")
        try:
            value = json.loads(self.payload_json)
            canonical = json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("hook payload must be valid JSON") from error
        if not isinstance(value, dict) or canonical != self.payload_json:
            raise ValueError("hook payload must be a canonical JSON object")
        return self


class HookResponse(StrictProtocolModel):
    event_id: EventId
    outcome: HookOutcome
    reason: BoundedReason
    mutation_json: Literal["{}"] = "{}"

    @model_validator(mode="after")
    def validate_no_mutation(self) -> Self:
        if self.mutation_json != "{}":
            raise ValueError("hook responses cannot mutate execution")
        return self


class HookDispatchResult(StrictProtocolModel):
    event_id: EventId
    allowed: bool
    security_outcomes: tuple[HookOutcome, ...] = Field(
        max_length=HOOK_MAX_REGISTRATIONS
    )
    advisory_failures: tuple[BoundedLabel, ...] = Field(
        max_length=HOOK_MAX_REGISTRATIONS
    )
    notification_failures: tuple[BoundedLabel, ...] = Field(
        max_length=HOOK_MAX_REGISTRATIONS
    )
    mutation_json: Literal["{}"] = "{}"

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        for values in (
            self.security_outcomes,
            self.advisory_failures,
            self.notification_failures,
        ):
            if tuple(sorted(values, key=str)) != values:
                raise ValueError("hook dispatch evidence must be sorted")
        return self
