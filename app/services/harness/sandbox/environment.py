"""Bounded secret-redacting child environment preparation."""

import struct
from collections.abc import Mapping

from app.services.harness.protocol.secret_delivery import SecretDeliveryReceipt
from app.services.harness.sandbox.models import SandboxProfile

MAXIMUM_SECRET_VALUE_BYTES = 64 * 1024
MAXIMUM_SECRET_PACKET_BYTES = 256 * 1024


class SandboxEnvironmentError(ValueError):
    """A child environment does not match its sandbox profile."""


class SandboxChildEnvironment:
    """Secret-redacting environment input bound to one operation destination."""

    def __init__(
        self,
        *,
        operation_id: str,
        destination_sha256: str,
        parent_environment: Mapping[str, str],
        secret_environment: Mapping[str, memoryview],
        receipt: SecretDeliveryReceipt | None,
    ) -> None:
        if len(parent_environment) > 64 or len(secret_environment) > 16:
            raise SandboxEnvironmentError(
                "sandbox environment exceeds its entry limit"
            )
        self.operation_id = operation_id
        self.destination_sha256 = destination_sha256
        self.parent_environment = dict(parent_environment)
        self.secret_environment = dict(secret_environment)
        self.receipt = receipt

    def __repr__(self) -> str:
        return (
            "SandboxChildEnvironment("
            f"operation_id={self.operation_id!r}, "
            f"parent_names={tuple(sorted(self.parent_environment))!r}, "
            f"secret_names={tuple(sorted(self.secret_environment))!r})"
        )


def prepare_child_environment(
    profile: SandboxProfile,
    environment: SandboxChildEnvironment,
) -> tuple[dict[str, str], bytearray]:
    if (
        environment.operation_id != profile.operation_id
        or environment.destination_sha256 != profile.destination_sha256
    ):
        raise SandboxEnvironmentError(
            "sandbox environment destination does not match"
        )
    parent_names = tuple(sorted(environment.parent_environment))
    if parent_names != profile.inherited_environment_names:
        raise SandboxEnvironmentError(
            "sandbox parent environment is not allowed"
        )
    for value in environment.parent_environment.values():
        if not isinstance(value, str) or "\x00" in value or len(value) > 4096:
            raise SandboxEnvironmentError(
                "sandbox parent environment value is invalid"
            )
    secret_names = tuple(sorted(environment.secret_environment))
    if secret_names != profile.secret_environment_names:
        raise SandboxEnvironmentError(
            "sandbox secret environment does not match"
        )
    if secret_names:
        receipt = environment.receipt
        if (
            receipt is None
            or receipt.operation_id != profile.operation_id
            or receipt.destination_sha256 != profile.destination_sha256
            or receipt.environment_names != secret_names
        ):
            raise SandboxEnvironmentError(
                "sandbox secret receipt does not match"
            )
    elif environment.receipt is not None:
        raise SandboxEnvironmentError(
            "sandbox received an unexpected secret receipt"
        )

    packet = bytearray(struct.pack(">H", len(secret_names)))
    for name in secret_names:
        view = environment.secret_environment[name]
        if (
            not isinstance(view, memoryview)
            or not view
            or len(view) > MAXIMUM_SECRET_VALUE_BYTES
            or 0 in view
            or not view.readonly
            or not view.c_contiguous
            or view.itemsize != 1
        ):
            _zero_packet(packet)
            raise SandboxEnvironmentError("sandbox secret value is invalid")
        name_bytes = name.encode("ascii")
        packet.extend(struct.pack(">H", len(name_bytes)))
        packet.extend(struct.pack(">I", len(view)))
        packet.extend(name_bytes)
        packet.extend(view)
        if len(packet) > MAXIMUM_SECRET_PACKET_BYTES:
            _zero_packet(packet)
            raise SandboxEnvironmentError(
                "sandbox secret packet exceeds its limit"
            )
    return dict(environment.parent_environment), packet


def _zero_packet(packet: bytearray) -> None:
    packet[:] = bytes(len(packet))
