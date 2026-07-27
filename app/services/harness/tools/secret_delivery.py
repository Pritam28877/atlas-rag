"""Bounded per-call secret acquisition with explicit parent filtering."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from app.services.harness.protocol.secret_delivery import (
    SecretDeliveryReceipt,
    SecretHandle,
    SecretInjectionRequest,
    secret_delivery_receipt_sha256,
    secret_handle_sha256,
)


class ChildSecretBackend(Protocol):
    async def resolve(
        self,
        handle: SecretHandle,
        *,
        destination_sha256: str,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> bytearray: ...


class ChildSecretDeliveryError(RuntimeError):
    """Sanitized secret-delivery failure without backend or value detail."""


class ChildSecretScope:
    """Owns mutable per-call values and zeros them exactly once."""

    def __init__(
        self,
        *,
        parent_environment: dict[str, str],
        secret_environment: dict[str, bytearray],
        receipt: SecretDeliveryReceipt,
    ) -> None:
        self.parent_environment = parent_environment
        self.receipt = receipt
        self._secret_environment = secret_environment
        self._released = False

    def secret_views(self) -> Mapping[str, memoryview]:
        if self._released:
            raise ChildSecretDeliveryError("child secret scope is released")
        return {
            name: memoryview(value).toreadonly()
            for name, value in self._secret_environment.items()
        }

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        for value in self._secret_environment.values():
            value[:] = bytes(len(value))
        self._secret_environment.clear()
        self.parent_environment.clear()

    async def __aenter__(self) -> ChildSecretScope:
        return self

    async def __aexit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> None:
        self.release()

    def __repr__(self) -> str:
        return (
            "ChildSecretScope("
            f"operation_id={self.receipt.operation_id!r}, "
            f"released={self._released})"
        )


class PerCallSecretBroker:
    def __init__(self, backend: ChildSecretBackend) -> None:
        self._backend = backend

    async def acquire(
        self,
        request: SecretInjectionRequest,
        *,
        parent_environment: Mapping[str, str],
        cancellation: asyncio.Event,
        deadline_at: datetime,
        delivered_at: datetime,
    ) -> ChildSecretScope:
        if cancellation.is_set():
            raise ChildSecretDeliveryError("child secret delivery cancelled")
        filtered_parent = {
            name: parent_environment[name]
            for name in request.inherited_environment_names
            if name in parent_environment
        }
        resolved: dict[str, bytearray] = {}
        try:
            for binding in request.bindings:
                if cancellation.is_set():
                    raise ChildSecretDeliveryError(
                        "child secret delivery cancelled"
                    )
                value = await self._backend.resolve(
                    binding.secret_handle,
                    destination_sha256=request.destination_sha256,
                    cancellation=cancellation,
                    deadline_at=deadline_at,
                )
                if cancellation.is_set():
                    value[:] = bytes(len(value))
                    raise ChildSecretDeliveryError(
                        "child secret delivery cancelled"
                    )
                if not value:
                    raise ChildSecretDeliveryError(
                        "child secret backend returned no value"
                    )
                resolved[binding.environment_name] = value
        except ChildSecretDeliveryError:
            _zero_values(resolved)
            filtered_parent.clear()
            raise
        except BaseException:
            _zero_values(resolved)
            filtered_parent.clear()
            raise ChildSecretDeliveryError(
                "child secret backend failed"
            ) from None

        handle_hashes = tuple(
            sorted(
                secret_handle_sha256(binding.secret_handle)
                for binding in request.bindings
            )
        )
        environment_names = tuple(sorted(resolved))
        receipt_sha256 = secret_delivery_receipt_sha256(
            operation_id=request.operation_id,
            destination_sha256=request.destination_sha256,
            secret_handle_sha256s=handle_hashes,
            environment_names=environment_names,
            delivered_at=delivered_at,
        )
        receipt = SecretDeliveryReceipt(
            operation_id=request.operation_id,
            destination_sha256=request.destination_sha256,
            secret_handle_sha256s=handle_hashes,
            environment_names=environment_names,
            delivered_at=delivered_at,
            receipt_sha256=receipt_sha256,
        )
        return ChildSecretScope(
            parent_environment=filtered_parent,
            secret_environment=resolved,
            receipt=receipt,
        )


def _zero_values(values: dict[str, bytearray]) -> None:
    for value in values.values():
        value[:] = bytes(len(value))
    values.clear()
