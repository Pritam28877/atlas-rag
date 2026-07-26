"""Bounded blocking boundary for explicit AWS credential sources."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Never, Protocol

from app.services.harness.providers.bedrock_identity import (
    BedrockIdentityReference,
)
from app.services.harness.providers.credential_material import zero_buffer

MAXIMUM_AWS_CREDENTIAL_BYTES = 16 * 1024


class BedrockCredentialResolutionErrorCode(StrEnum):
    CANCELLED = "cancelled"
    CLOSED = "closed"
    DEADLINE = "deadline"
    INVALID = "invalid"
    LOAD = "load"


class BedrockCredentialResolutionError(RuntimeError):
    def __init__(self, code: BedrockCredentialResolutionErrorCode) -> None:
        super().__init__("Bedrock credential resolution failed")
        self.code = code


class BedrockCredentialMaterial:
    """Mutable credential bytes with a redacted representation."""

    __slots__ = (
        "_access_key_id",
        "_secret_access_key",
        "_session_token",
        "expires_at",
    )

    def __init__(
        self,
        access_key_id: bytearray,
        secret_access_key: bytearray,
        session_token: bytearray | None,
        *,
        expires_at: datetime | None,
    ) -> None:
        values = tuple(
            value
            for value in (
                access_key_id,
                secret_access_key,
                session_token,
            )
            if value is not None
        )
        if (
            any(not isinstance(value, bytearray) for value in values)
            or not 1
            <= sum(len(value) for value in values)
            <= MAXIMUM_AWS_CREDENTIAL_BYTES
            or not access_key_id
            or not secret_access_key
        ):
            for value in values:
                zero_buffer(value)
            raise BedrockCredentialResolutionError(
                BedrockCredentialResolutionErrorCode.INVALID
            )
        if expires_at is not None and (
            expires_at.tzinfo is None or expires_at.utcoffset() != timedelta(0)
        ):
            for value in values:
                zero_buffer(value)
            raise BedrockCredentialResolutionError(
                BedrockCredentialResolutionErrorCode.INVALID
            )
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._session_token = session_token
        self.expires_at = expires_at

    def views(self) -> tuple[memoryview, memoryview, memoryview | None]:
        return (
            memoryview(self._access_key_id).toreadonly(),
            memoryview(self._secret_access_key).toreadonly(),
            (
                memoryview(self._session_token).toreadonly()
                if self._session_token is not None
                else None
            ),
        )

    def zero(self) -> None:
        zero_buffer(self._access_key_id)
        zero_buffer(self._secret_access_key)
        if self._session_token is not None:
            zero_buffer(self._session_token)

    def __repr__(self) -> str:
        return "BedrockCredentialMaterial(<redacted>)"


class BedrockBlockingCredentialBackend(Protocol):
    def load(
        self,
        identity: BedrockIdentityReference,
    ) -> BedrockCredentialMaterial: ...


class BoundedBedrockCredentialResolver:
    def __init__(
        self,
        backend: BedrockBlockingCredentialBackend,
        *,
        clock: Callable[[], datetime],
        maximum_workers: int = 2,
        maximum_pending: int = 8,
    ) -> None:
        if not 1 <= maximum_workers <= 8:
            raise ValueError("Bedrock credential worker limit is invalid")
        if not maximum_workers <= maximum_pending <= 64:
            raise ValueError("Bedrock credential pending limit is invalid")
        self._backend = backend
        self._clock = clock
        self._executor = ThreadPoolExecutor(
            max_workers=maximum_workers,
            thread_name_prefix="atlas-bedrock-credentials",
        )
        self._capacity = asyncio.Semaphore(maximum_pending)
        self._closed = False

    async def resolve(
        self,
        identity: BedrockIdentityReference,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> BedrockCredentialMaterial:
        if self._closed:
            self._reject(BedrockCredentialResolutionErrorCode.CLOSED)
        remaining = self._remaining(deadline_at)
        if cancellation.is_set():
            self._reject(BedrockCredentialResolutionErrorCode.CANCELLED)
        try:
            async with asyncio.timeout(remaining):
                async with self._capacity:
                    return await self._load(
                        identity,
                        cancellation,
                    )
        except TimeoutError:
            self._reject(BedrockCredentialResolutionErrorCode.DEADLINE)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(
            self._executor.shutdown,
            wait=True,
            cancel_futures=True,
        )

    async def _load(
        self,
        identity: BedrockIdentityReference,
        cancellation: asyncio.Event,
    ) -> BedrockCredentialMaterial:
        loop = asyncio.get_running_loop()
        load_future = loop.run_in_executor(
            self._executor,
            self._backend.load,
            identity,
        )
        cancellation_task = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                (load_future, cancellation_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task in done:
                load_future.add_done_callback(_zero_future_material)
                self._reject(BedrockCredentialResolutionErrorCode.CANCELLED)
            try:
                return load_future.result()
            except BedrockCredentialResolutionError:
                raise
            except Exception:
                self._reject(BedrockCredentialResolutionErrorCode.LOAD)
        except asyncio.CancelledError:
            load_future.add_done_callback(_zero_future_material)
            raise
        finally:
            cancellation_task.cancel()
            await asyncio.gather(cancellation_task, return_exceptions=True)

    def _remaining(self, deadline_at: datetime) -> float:
        now = self._clock()
        if (
            now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or deadline_at.tzinfo is None
            or deadline_at.utcoffset() != timedelta(0)
            or deadline_at <= now
        ):
            self._reject(BedrockCredentialResolutionErrorCode.DEADLINE)
        return (deadline_at - now).total_seconds()

    @staticmethod
    def _reject(code: BedrockCredentialResolutionErrorCode) -> Never:
        raise BedrockCredentialResolutionError(code)


def _zero_future_material(
    future: asyncio.Future[BedrockCredentialMaterial],
) -> None:
    if not future.cancelled() and future.exception() is None:
        future.result().zero()
