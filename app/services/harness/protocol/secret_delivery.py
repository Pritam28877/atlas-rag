"""Secret-free contracts for destination-bound child delivery."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol.base import (
    OperationId,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)

type ChildEnvironmentName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Z_][A-Z0-9_]*$",
    ),
]
type SecretHandle = Annotated[
    str,
    StringConstraints(
        min_length=8,
        max_length=256,
        pattern=r"^secret:[a-z0-9][a-z0-9._/-]+$",
    ),
]


class ChildSecretBinding(StrictProtocolModel):
    secret_handle: SecretHandle
    environment_name: ChildEnvironmentName


class SecretInjectionRequest(StrictProtocolModel):
    operation_id: OperationId
    destination_sha256: Sha256
    bindings: tuple[ChildSecretBinding, ...] = Field(min_length=1, max_length=16)
    inherited_environment_names: tuple[ChildEnvironmentName, ...] = Field(
        max_length=64
    )

    @model_validator(mode="after")
    def validate_canonical_bindings(self) -> Self:
        binding_keys = tuple(
            (binding.environment_name, binding.secret_handle)
            for binding in self.bindings
        )
        if tuple(sorted(set(binding_keys))) != binding_keys:
            raise ValueError("secret bindings must be unique and sorted")
        names = tuple(binding.environment_name for binding in self.bindings)
        if len(names) != len(set(names)):
            raise ValueError("secret environment names must be unique")
        if tuple(sorted(set(self.inherited_environment_names))) != (
            self.inherited_environment_names
        ):
            raise ValueError("inherited environment names must be unique and sorted")
        if set(names).intersection(self.inherited_environment_names):
            raise ValueError("secret names cannot be inherited from parent")
        return self


class SecretDeliveryReceipt(StrictProtocolModel):
    operation_id: OperationId
    destination_sha256: Sha256
    secret_handle_sha256s: tuple[Sha256, ...] = Field(
        min_length=1,
        max_length=16,
    )
    environment_names: tuple[ChildEnvironmentName, ...] = Field(
        min_length=1,
        max_length=16,
    )
    delivered_at: UtcTimestamp
    receipt_sha256: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if tuple(sorted(set(self.secret_handle_sha256s))) != (
            self.secret_handle_sha256s
        ):
            raise ValueError("secret handle hashes must be unique and sorted")
        if tuple(sorted(set(self.environment_names))) != self.environment_names:
            raise ValueError("secret environment names must be unique and sorted")
        expected = secret_delivery_receipt_sha256(
            operation_id=self.operation_id,
            destination_sha256=self.destination_sha256,
            secret_handle_sha256s=self.secret_handle_sha256s,
            environment_names=self.environment_names,
            delivered_at=self.delivered_at,
        )
        if self.receipt_sha256 != expected:
            raise ValueError("secret delivery receipt hash is invalid")
        return self


def secret_handle_sha256(handle: str) -> str:
    return hashlib.sha256(handle.encode()).hexdigest()


def secret_delivery_receipt_sha256(**values: object) -> str:
    encoded = json.dumps(
        values,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=lambda value: value.isoformat(),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
