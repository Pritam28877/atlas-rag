"""Destination-bound, cancellation-safe credential lease broker."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime

from app.services.harness.protocol import (
    ProviderCredentialHandle,
    ProviderName,
    Sha256,
)
from app.services.harness.providers.config_contracts import (
    LoadedProviderConfiguration,
)
from app.services.harness.providers.credential_material import (
    CredentialBrokerError,
    CredentialBrokerErrorCode,
    CredentialLease,
    CredentialSecretBackend,
    CredentialSecretMaterial,
    require_utc,
    zero_buffer,
)

MAXIMUM_ACTIVE_CREDENTIAL_LEASES = 256


class ConfiguredCredentialBroker:
    """Implements the provider credential trait with strict binding checks."""

    def __init__(
        self,
        loaded: LoadedProviderConfiguration,
        backend: CredentialSecretBackend,
        *,
        clock: Callable[[], datetime],
        maximum_active_leases: int = MAXIMUM_ACTIVE_CREDENTIAL_LEASES,
    ) -> None:
        if not 1 <= maximum_active_leases <= MAXIMUM_ACTIVE_CREDENTIAL_LEASES:
            raise ValueError("active credential lease limit is invalid")
        self._bindings = {
            binding.handle: binding
            for binding in loaded.configuration.credential_bindings
        }
        self._backend = backend
        self._clock = clock
        self._maximum_active_leases = maximum_active_leases
        self._broker_identity = object()
        self._next_lease_id = 1
        self._active: dict[int, CredentialLease] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        handle: ProviderCredentialHandle,
        *,
        provider: ProviderName,
        destination_sha256: Sha256,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> CredentialLease:
        return await self._acquire(
            handle,
            provider=provider,
            destination_sha256=destination_sha256,
            cancellation=cancellation,
            deadline_at=deadline_at,
            replacement=None,
        )

    async def _acquire(
        self,
        handle: ProviderCredentialHandle,
        *,
        provider: ProviderName,
        destination_sha256: Sha256,
        cancellation: asyncio.Event,
        deadline_at: datetime,
        replacement: CredentialLease | None,
    ) -> CredentialLease:
        binding = self._bindings.get(handle)
        if binding is None:
            raise CredentialBrokerError(
                CredentialBrokerErrorCode.UNKNOWN_HANDLE
            )
        if binding.provider != provider:
            raise CredentialBrokerError(
                CredentialBrokerErrorCode.PROVIDER_MISMATCH
            )
        if binding.destination_sha256 != destination_sha256:
            raise CredentialBrokerError(
                CredentialBrokerErrorCode.DESTINATION_MISMATCH
            )
        material = await self._load_material(
            handle,
            cancellation=cancellation,
            deadline_at=deadline_at,
        )
        secret = material.claim()
        try:
            now = self._now()
            if material.expires_at <= now:
                raise CredentialBrokerError(
                    CredentialBrokerErrorCode.EXPIRED
                )
            async with self._lock:
                if replacement is None:
                    retained_lease_count = len(self._active)
                else:
                    if (
                        self._active.get(replacement._lease_id)
                        is not replacement
                    ):
                        raise CredentialBrokerError(
                            CredentialBrokerErrorCode.RELEASED
                        )
                    retained_lease_count = len(self._active) - 1
                if retained_lease_count >= self._maximum_active_leases:
                    raise CredentialBrokerError(
                        CredentialBrokerErrorCode.ACTIVE_LIMIT
                    )
                lease_id = self._next_lease_id
                self._next_lease_id += 1
                lease = CredentialLease(
                    broker_identity=self._broker_identity,
                    lease_id=lease_id,
                    handle=handle,
                    provider=provider,
                    destination_sha256=destination_sha256,
                    expires_at=material.expires_at,
                    secret=secret,
                )
                self._active[lease_id] = lease
                if replacement is not None:
                    self._active.pop(replacement._lease_id)
                    replacement.release()
                return lease
        except BaseException:
            zero_buffer(secret)
            raise

    async def refresh(
        self,
        credential: CredentialLease,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> CredentialLease:
        self._require_owned_active(credential)
        return await self._acquire(
            credential.handle,
            provider=credential.provider,
            destination_sha256=credential.destination_sha256,
            cancellation=cancellation,
            deadline_at=deadline_at,
            replacement=credential,
        )

    async def release(self, credential: CredentialLease) -> None:
        self._require_owned(credential)
        async with self._lock:
            active = self._active.pop(credential._lease_id, None)
            if active is not None:
                active.release()

    async def active_leases(self) -> int:
        async with self._lock:
            return len(self._active)

    async def _load_material(
        self,
        handle: ProviderCredentialHandle,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> CredentialSecretMaterial:
        now = self._now()
        require_utc(deadline_at, "credential deadline")
        if cancellation.is_set():
            raise CredentialBrokerError(CredentialBrokerErrorCode.CANCELLED)
        remaining_seconds = (deadline_at - now).total_seconds()
        if remaining_seconds <= 0:
            raise CredentialBrokerError(CredentialBrokerErrorCode.DEADLINE)

        load_task = asyncio.create_task(self._backend.load(handle))
        cancellation_task = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                (load_task, cancellation_task),
                timeout=remaining_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task in done:
                _zero_completed_material(load_task)
                await _cancel_task(load_task)
                raise CredentialBrokerError(
                    CredentialBrokerErrorCode.CANCELLED
                )
            if load_task not in done:
                await _cancel_task(load_task)
                raise CredentialBrokerError(
                    CredentialBrokerErrorCode.DEADLINE
                )
            return load_task.result()
        except CredentialBrokerError:
            raise
        except asyncio.CancelledError:
            await _cancel_task(load_task)
            raise
        except Exception:
            raise CredentialBrokerError(
                CredentialBrokerErrorCode.BACKEND
            ) from None
        finally:
            await _cancel_task(cancellation_task)

    def _now(self) -> datetime:
        now = self._clock()
        require_utc(now, "credential broker clock")
        return now

    def _require_owned(self, credential: CredentialLease) -> None:
        if credential._broker_identity is not self._broker_identity:
            raise CredentialBrokerError(
                CredentialBrokerErrorCode.UNKNOWN_HANDLE
            )

    def _require_owned_active(self, credential: CredentialLease) -> None:
        self._require_owned(credential)
        if credential.released:
            raise CredentialBrokerError(CredentialBrokerErrorCode.RELEASED)


async def _cancel_task(task: asyncio.Task[object]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _zero_completed_material(
    task: asyncio.Task[CredentialSecretMaterial],
) -> None:
    if task.done() and not task.cancelled() and task.exception() is None:
        task.result().zero()
