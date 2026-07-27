"""Filtered parent environment and zeroed per-call secret tests."""

import asyncio
from datetime import timedelta

import pytest

from app.services.harness.protocol.secret_delivery import (
    ChildSecretBinding,
    SecretInjectionRequest,
)
from app.services.harness.tools.secret_delivery import (
    ChildSecretDeliveryError,
    PerCallSecretBroker,
)
from tests.harness.operations.fixtures import NOW, OPERATION_ID


class Backend:
    def __init__(self, *, fail_second: bool = False) -> None:
        self.fail_second = fail_second
        self.values: list[bytearray] = []
        self.destinations: list[str] = []

    async def resolve(
        self,
        handle,
        *,
        destination_sha256,
        cancellation,
        deadline_at,
    ):
        if self.fail_second and self.values:
            raise RuntimeError("backend detail must not escape")
        value = bytearray(f"value-{len(self.values) + 1}".encode())
        self.values.append(value)
        self.destinations.append(destination_sha256)
        return value


def request() -> SecretInjectionRequest:
    return SecretInjectionRequest(
        operation_id=OPERATION_ID,
        destination_sha256="a" * 64,
        bindings=(
            ChildSecretBinding(
                secret_handle="secret:provider/api-key",
                environment_name="PROVIDER_API_KEY",
            ),
            ChildSecretBinding(
                secret_handle="secret:provider/session-token",
                environment_name="PROVIDER_SESSION_TOKEN",
            ),
        ),
        inherited_environment_names=("LANG", "PATH"),
    )


def test_only_allowlisted_parent_and_requested_secrets_enter_scope() -> None:
    async def scenario() -> None:
        backend = Backend()
        scope = await PerCallSecretBroker(backend).acquire(
            request(),
            parent_environment={
                "PATH": "/usr/bin",
                "LANG": "C.UTF-8",
                "AWS_SECRET_ACCESS_KEY": "parent-secret",
            },
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=1),
            delivered_at=NOW,
        )
        views = scope.secret_views()

        assert scope.parent_environment == {
            "LANG": "C.UTF-8",
            "PATH": "/usr/bin",
        }
        assert set(views) == {"PROVIDER_API_KEY", "PROVIDER_SESSION_TOKEN"}
        assert "parent-secret" not in repr(scope)
        assert "secret:provider" not in scope.receipt.model_dump_json()
        scope.release()
        assert all(value == bytearray(len(value)) for value in backend.values)
        with pytest.raises(ChildSecretDeliveryError, match="released"):
            scope.secret_views()

    asyncio.run(scenario())


def test_partial_backend_failure_zeros_acquired_values() -> None:
    async def scenario() -> None:
        backend = Backend(fail_second=True)
        with pytest.raises(ChildSecretDeliveryError) as failure:
            await PerCallSecretBroker(backend).acquire(
                request(),
                parent_environment={"PATH": "/usr/bin"},
                cancellation=asyncio.Event(),
                deadline_at=NOW + timedelta(seconds=1),
                delivered_at=NOW,
            )

        assert "backend detail" not in str(failure.value)
        assert backend.values[0] == bytearray(len(backend.values[0]))

    asyncio.run(scenario())


def test_secret_names_cannot_be_inherited_from_parent() -> None:
    with pytest.raises(ValueError, match="cannot be inherited"):
        SecretInjectionRequest(
            operation_id=OPERATION_ID,
            destination_sha256="a" * 64,
            bindings=(
                ChildSecretBinding(
                    secret_handle="secret:provider/api-key",
                    environment_name="PROVIDER_API_KEY",
                ),
            ),
            inherited_environment_names=("PROVIDER_API_KEY",),
        )


def test_cancellation_after_resolution_zeros_returned_value() -> None:
    class CancellingBackend(Backend):
        async def resolve(
            self,
            handle,
            *,
            destination_sha256,
            cancellation,
            deadline_at,
        ):
            value = await super().resolve(
                handle,
                destination_sha256=destination_sha256,
                cancellation=cancellation,
                deadline_at=deadline_at,
            )
            cancellation.set()
            return value

    async def scenario() -> None:
        backend = CancellingBackend()
        with pytest.raises(ChildSecretDeliveryError, match="cancelled"):
            await PerCallSecretBroker(backend).acquire(
                request(),
                parent_environment={},
                cancellation=asyncio.Event(),
                deadline_at=NOW + timedelta(seconds=1),
                delivered_at=NOW,
            )
        assert backend.values[0] == bytearray(len(backend.values[0]))

    asyncio.run(scenario())
