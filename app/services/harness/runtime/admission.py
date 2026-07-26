"""Bounded request admission, cancellation, deadline, and command decoding."""

from __future__ import annotations

import asyncio
import time
from enum import StrEnum
from types import TracebackType

from pydantic import ValidationError

from app.services.harness.protocol import CommandEnvelope


class AdmissionErrorCode(StrEnum):
    MALFORMED_COMMAND = "malformed_command"
    CANCELLED = "cancelled"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    DEADLINE_TOO_LARGE = "deadline_too_large"


class AdmissionError(RuntimeError):
    """Client-safe admission failure with no internal exception disclosure."""

    def __init__(self, code: AdmissionErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def decode_command(payload: bytes) -> CommandEnvelope:
    try:
        return CommandEnvelope.model_validate_json(payload)
    except ValidationError as error:
        raise AdmissionError(
            AdmissionErrorCode.MALFORMED_COMMAND,
            "request is not a valid command envelope",
        ) from error


class AdmissionLease:
    """Exactly-once release token for one admitted request."""

    def __init__(self, semaphore: asyncio.BoundedSemaphore) -> None:
        self._semaphore = semaphore
        self._released = False

    async def __aenter__(self) -> AdmissionLease:
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._semaphore.release()


class RequestAdmission:
    """Owns a bounded semaphore and every task used while waiting for it."""

    def __init__(
        self,
        *,
        maximum_concurrency: int,
        maximum_request_seconds: float,
    ) -> None:
        if not 1 <= maximum_concurrency <= 64:
            raise ValueError("maximum_concurrency must be between 1 and 64")
        if not 0.001 <= maximum_request_seconds <= 3600:
            raise ValueError("maximum_request_seconds must be between 1 ms and 1 hour")
        self._semaphore = asyncio.BoundedSemaphore(maximum_concurrency)
        self._maximum_request_seconds = maximum_request_seconds

    async def acquire(
        self,
        *,
        deadline_monotonic: float,
        cancellation_event: asyncio.Event,
    ) -> AdmissionLease:
        remaining_seconds = deadline_monotonic - time.monotonic()
        if remaining_seconds <= 0:
            raise AdmissionError(
                AdmissionErrorCode.DEADLINE_EXCEEDED,
                "request deadline has elapsed",
            )
        if remaining_seconds > self._maximum_request_seconds:
            raise AdmissionError(
                AdmissionErrorCode.DEADLINE_TOO_LARGE,
                "request deadline exceeds configured maximum",
            )
        if cancellation_event.is_set():
            raise AdmissionError(
                AdmissionErrorCode.CANCELLED,
                "request was cancelled before admission",
            )

        acquire_task = asyncio.create_task(self._semaphore.acquire())
        cancellation_task = asyncio.create_task(cancellation_event.wait())
        permit_transferred = False
        try:
            done, _ = await asyncio.wait(
                {acquire_task, cancellation_task},
                timeout=remaining_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task in done and cancellation_task.result():
                raise AdmissionError(
                    AdmissionErrorCode.CANCELLED,
                    "request was cancelled while awaiting admission",
                )
            if acquire_task in done and acquire_task.result():
                permit_transferred = True
                return AdmissionLease(self._semaphore)
            raise AdmissionError(
                AdmissionErrorCode.DEADLINE_EXCEEDED,
                "request deadline elapsed while awaiting admission",
            )
        finally:
            for task in (acquire_task, cancellation_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                acquire_task,
                cancellation_task,
                return_exceptions=True,
            )
            if not permit_transferred and not acquire_task.cancelled():
                if acquire_task.exception() is None and acquire_task.result():
                    self._semaphore.release()
