"""Fail-closed security and non-mutating advisory hook dispatcher."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.services.harness.extensions.hook_contracts import (
    HOOK_MAX_REGISTRATIONS,
    HookDescriptor,
    HookDispatchResult,
    HookEvent,
    HookFailurePolicy,
    HookMode,
    HookOutcome,
    HookResponse,
)


class HookTransport(Protocol):
    async def invoke(
        self,
        event: HookEvent,
    ) -> bytes | str | Mapping[str, object]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class HookRegistration:
    descriptor: HookDescriptor
    transport: HookTransport


class HookDispatchErrorCode(StrEnum):
    INVALID_EVENT = "invalid_event"
    SECURITY_FAILURE = "security_failure"
    TRANSPORT = "transport"


class HookDispatchError(RuntimeError):
    def __init__(self, code: HookDispatchErrorCode) -> None:
        super().__init__("hook dispatch failed")
        self.code = code


class HookDispatcher:
    def __init__(self, registrations: tuple[HookRegistration, ...]) -> None:
        if len(registrations) > HOOK_MAX_REGISTRATIONS:
            raise ValueError("hook registration count exceeds limit")
        identities = tuple(
            (entry.descriptor.name, entry.descriptor.version)
            for entry in registrations
        )
        if identities != tuple(sorted(set(identities))):
            raise ValueError("hook registrations must be unique and sorted")
        self._registrations = registrations

    async def dispatch(
        self,
        event: HookEvent,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> HookDispatchResult:
        verified_event = HookEvent.model_validate(event.model_dump())
        security_outcomes: list[HookOutcome] = []
        advisory_failures: list[str] = []
        notification_failures: list[str] = []
        allowed = True
        for registration in self._registrations:
            descriptor = registration.descriptor
            if verified_event.event_type not in descriptor.events:
                continue
            try:
                response = await self._invoke(
                    registration,
                    verified_event,
                    cancellation,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if descriptor.failure_policy is HookFailurePolicy.FAIL_CLOSED:
                    allowed = False
                    raise HookDispatchError(
                        HookDispatchErrorCode.SECURITY_FAILURE
                    ) from None
                if descriptor.mode is HookMode.ADVISORY:
                    advisory_failures.append(descriptor.name)
                else:
                    notification_failures.append(descriptor.name)
                continue
            if descriptor.mode is HookMode.SECURITY:
                security_outcomes.append(response.outcome)
                if response.outcome is not HookOutcome.ALLOW:
                    allowed = False
            elif descriptor.mode is HookMode.ADVISORY:
                if response.outcome is HookOutcome.DENY:
                    advisory_failures.append(descriptor.name)
            elif response.outcome is HookOutcome.DENY:
                notification_failures.append(descriptor.name)
        return HookDispatchResult(
            event_id=verified_event.event_id,
            allowed=allowed,
            security_outcomes=tuple(sorted(security_outcomes, key=str)),
            advisory_failures=tuple(sorted(advisory_failures)),
            notification_failures=tuple(sorted(notification_failures)),
        )

    async def close(self) -> None:
        failures: list[BaseException] = []
        for registration in self._registrations:
            try:
                await registration.transport.close()
            except BaseException as error:
                failures.append(error)
        if failures:
            raise HookDispatchError(HookDispatchErrorCode.TRANSPORT) from failures[0]

    async def _invoke(
        self,
        registration: HookRegistration,
        event: HookEvent,
        cancellation: asyncio.Event | None,
    ) -> HookResponse:
        invocation = asyncio.create_task(registration.transport.invoke(event))
        cancellation_task = (
            asyncio.create_task(cancellation.wait())
            if cancellation is not None
            else None
        )
        wait_set: set[asyncio.Task[object]] = {invocation}
        if cancellation_task is not None:
            wait_set.add(cancellation_task)
        try:
            done, _ = await asyncio.wait(
                wait_set,
                timeout=registration.descriptor.timeout_ms / 1000,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done or (
                cancellation_task is not None and cancellation_task in done
            ):
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)
                raise HookDispatchError(HookDispatchErrorCode.TRANSPORT)
            raw_response = await invocation
            if isinstance(raw_response, bytes):
                import json

                parsed = json.loads(raw_response.decode("utf-8"))
            elif isinstance(raw_response, str):
                import json

                parsed = json.loads(raw_response)
            else:
                parsed = dict(raw_response)
            response = HookResponse.model_validate(parsed)
            if response.event_id != event.event_id:
                raise HookDispatchError(HookDispatchErrorCode.INVALID_EVENT)
            return response
        finally:
            if cancellation_task is not None:
                cancellation_task.cancel()
                await asyncio.gather(cancellation_task, return_exceptions=True)
