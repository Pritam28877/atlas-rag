"""Immutable bounded tool lookup and fail-closed argument validation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from app.services.harness.protocol.operation_admission import (
    MAXIMUM_ARGUMENT_BYTES,
    canonical_operation_args_sha256,
)
from app.services.harness.protocol.provider_stream import ProviderToolCall
from app.services.harness.tools.contracts import (
    StrictToolArguments,
    ToolDescriptor,
    ValidatedToolCall,
    canonical_tool_schema,
)

MAXIMUM_REGISTERED_TOOLS = 256


class ToolRegistryErrorCode(StrEnum):
    ARGUMENT_LIMIT = "argument_limit"
    INVALID_ARGUMENTS = "invalid_arguments"
    UNKNOWN_TOOL = "unknown_tool"


class ToolRegistryError(RuntimeError):
    def __init__(self, code: ToolRegistryErrorCode) -> None:
        super().__init__("tool registry rejected the call")
        self.code = code


class ToolRegistrationError(ValueError):
    """A trusted registry definition is ambiguous or inconsistent."""


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    descriptor: ToolDescriptor
    argument_model: type[StrictToolArguments]

    def __post_init__(self) -> None:
        try:
            descriptor = ToolDescriptor.model_validate(
                self.descriptor.model_dump()
            )
            schema_json = canonical_tool_schema(self.argument_model)
        except Exception as error:
            raise ToolRegistrationError(
                "tool registration contract is invalid"
            ) from error
        object.__setattr__(self, "descriptor", descriptor)
        if (
            schema_json != descriptor.input_schema_json
            or hashlib.sha256(schema_json.encode()).hexdigest()
            != descriptor.input_schema_sha256
        ):
            raise ToolRegistrationError(
                "tool registration schema does not match descriptor"
            )


class ToolRegistry:
    def __init__(self, registrations: tuple[ToolRegistration, ...]) -> None:
        if (
            not registrations
            or len(registrations) > MAXIMUM_REGISTERED_TOOLS
        ):
            raise ToolRegistrationError(
                "tool registry size is outside approved bounds"
            )
        identities = tuple(
            (registration.descriptor.name, registration.descriptor.version)
            for registration in registrations
        )
        if identities != tuple(sorted(set(identities))):
            raise ToolRegistrationError(
                "tool registrations must be unique and sorted"
            )

        canonical_names = {
            registration.descriptor.name for registration in registrations
        }
        by_identity: dict[tuple[str, str], ToolRegistration] = {}
        alias_owners: dict[str, str] = {}
        defaults: dict[str, ToolRegistration] = {}
        default_counts = {name: 0 for name in canonical_names}
        for registration in registrations:
            descriptor = registration.descriptor
            by_identity[(descriptor.name, descriptor.version)] = registration
            for alias in descriptor.aliases:
                if alias in canonical_names:
                    raise ToolRegistrationError(
                        "tool alias collides with a canonical name"
                    )
                owner = alias_owners.setdefault(alias, descriptor.name)
                if owner != descriptor.name:
                    raise ToolRegistrationError(
                        "tool alias resolves to multiple tools"
                    )
                by_identity[(alias, descriptor.version)] = registration
            if descriptor.default_version:
                default_counts[descriptor.name] += 1
                defaults[descriptor.name] = registration
                for alias in descriptor.aliases:
                    defaults[alias] = registration
        if any(count != 1 for count in default_counts.values()):
            raise ToolRegistrationError(
                "each tool requires exactly one default version"
            )

        self._registrations = registrations
        self._by_identity: Mapping[
            tuple[str, str], ToolRegistration
        ] = MappingProxyType(by_identity)
        self._defaults: Mapping[str, ToolRegistration] = MappingProxyType(
            defaults
        )

    @property
    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        return tuple(
            registration.descriptor for registration in self._registrations
        )

    def registration(
        self,
        name: str,
        *,
        version: str | None = None,
    ) -> ToolRegistration:
        registration = (
            self._defaults.get(name)
            if version is None
            else self._by_identity.get((name, version))
        )
        if registration is None:
            raise ToolRegistryError(ToolRegistryErrorCode.UNKNOWN_TOOL)
        return registration

    def validate_provider_call(
        self,
        call: ProviderToolCall,
        *,
        version: str | None = None,
    ) -> ValidatedToolCall:
        try:
            verified_call = ProviderToolCall.model_validate(call.model_dump())
        except Exception:
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID_ARGUMENTS
            ) from None
        registration = self.registration(
            verified_call.tool_name,
            version=version,
        )
        normalized_json, args_sha256 = _validate_arguments(
            verified_call.arguments_json,
            registration.argument_model,
        )
        descriptor = registration.descriptor
        return ValidatedToolCall(
            call_id=verified_call.call_id,
            requested_name=verified_call.tool_name,
            tool_name=descriptor.name,
            tool_version=descriptor.version,
            descriptor_sha256=descriptor.descriptor_sha256,
            arguments_json=normalized_json,
            args_sha256=args_sha256,
            capability=descriptor.capability,
            idempotency_class=descriptor.idempotency_class,
            output=descriptor.output,
        )

    def revalidate_call(
        self,
        call: ValidatedToolCall,
    ) -> ValidatedToolCall:
        try:
            verified = ValidatedToolCall.model_validate(call.model_dump())
            registration = self.registration(
                verified.tool_name,
                version=verified.tool_version,
            )
            requested_registration = self.registration(
                verified.requested_name,
                version=verified.tool_version,
            )
            normalized_json, args_sha256 = _validate_arguments(
                verified.arguments_json,
                registration.argument_model,
            )
        except ToolRegistryError:
            raise
        except Exception:
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID_ARGUMENTS
            ) from None
        descriptor = registration.descriptor
        if (
            requested_registration is not registration
            or normalized_json != verified.arguments_json
            or args_sha256 != verified.args_sha256
            or descriptor.name != verified.tool_name
            or descriptor.version != verified.tool_version
            or descriptor.descriptor_sha256 != verified.descriptor_sha256
            or descriptor.capability != verified.capability
            or descriptor.idempotency_class
            is not verified.idempotency_class
            or descriptor.output != verified.output
        ):
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID_ARGUMENTS
            )
        return verified


def _validate_arguments(
    arguments_json: str,
    argument_model: type[StrictToolArguments],
) -> tuple[str, str]:
    if len(arguments_json.encode()) > MAXIMUM_ARGUMENT_BYTES:
        raise ToolRegistryError(ToolRegistryErrorCode.ARGUMENT_LIMIT)
    try:
        arguments = json.loads(arguments_json)
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        canonical_input = json.dumps(
            arguments,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if canonical_input != arguments_json:
            raise ValueError("tool arguments must use canonical JSON")
        canonical_operation_args_sha256(arguments)
        validated = argument_model.model_validate_json(arguments_json)
        normalized = validated.model_dump(mode="json")
        normalized_json = json.dumps(
            normalized,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(normalized_json.encode()) > MAXIMUM_ARGUMENT_BYTES:
            raise ToolRegistryError(ToolRegistryErrorCode.ARGUMENT_LIMIT)
        args_sha256 = canonical_operation_args_sha256(normalized)
    except ToolRegistryError:
        raise
    except Exception:
        raise ToolRegistryError(
            ToolRegistryErrorCode.INVALID_ARGUMENTS
        ) from None
    return normalized_json, args_sha256
