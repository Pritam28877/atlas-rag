"""Progressive tool catalog discovery and schema materialization."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from app.services.harness.protocol.base import Capability, Sha256, StrictProtocolModel
from app.services.harness.tools.contracts import ToolDescriptor
from app.services.harness.tools.registry import ToolRegistry, ToolRegistryError

MAXIMUM_DISCOVERY_RESULTS = 256


class ToolCatalogSummary(StrictProtocolModel):
    name: str
    version: str
    capability: Capability
    descriptor_sha256: Sha256
    input_schema_sha256: Sha256
    output_media_type: str
    maximum_output_bytes: int = Field(ge=1, le=16 * 1024 * 1024)


class ToolDiscoveryRequest(StrictProtocolModel):
    allowed_capabilities: tuple[Capability, ...] = Field(max_length=256)
    limit: int = Field(default=64, ge=1, le=MAXIMUM_DISCOVERY_RESULTS)


class ToolDiscoveryResult(StrictProtocolModel):
    summaries: tuple[ToolCatalogSummary, ...] = Field(
        max_length=MAXIMUM_DISCOVERY_RESULTS
    )
    omitted_count: int = Field(ge=0, le=MAXIMUM_DISCOVERY_RESULTS)


class ToolDiscoveryErrorCode(StrEnum):
    CAPABILITY_DENIED = "capability_denied"
    UNKNOWN_TOOL = "unknown_tool"


class ToolDiscoveryError(RuntimeError):
    def __init__(self, code: ToolDiscoveryErrorCode) -> None:
        super().__init__("tool discovery rejected the request")
        self.code = code


class ProgressiveToolDiscovery:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def compact(self, request: ToolDiscoveryRequest) -> ToolDiscoveryResult:
        verified = ToolDiscoveryRequest.model_validate(request.model_dump())
        allowed = set(verified.allowed_capabilities)
        summaries = tuple(
            _summary(descriptor)
            for descriptor in self._registry.descriptors
            if descriptor.capability in allowed
        )
        return ToolDiscoveryResult(
            summaries=summaries[: verified.limit],
            omitted_count=max(0, len(summaries) - verified.limit),
        )

    def materialize(
        self,
        name: str,
        *,
        version: str | None,
        allowed_capabilities: tuple[Capability, ...],
    ) -> ToolDescriptor:
        try:
            if version is None:
                registration = self._registry.registration(name)
            else:
                registration = self._registry.registration(name, version=version)
        except ToolRegistryError:
            raise ToolDiscoveryError(ToolDiscoveryErrorCode.UNKNOWN_TOOL) from None
        if registration.descriptor.capability not in set(allowed_capabilities):
            raise ToolDiscoveryError(ToolDiscoveryErrorCode.CAPABILITY_DENIED)
        return ToolDescriptor.model_validate(registration.descriptor.model_dump())


def _summary(descriptor: ToolDescriptor) -> ToolCatalogSummary:
    return ToolCatalogSummary(
        name=descriptor.name,
        version=descriptor.version,
        capability=descriptor.capability,
        descriptor_sha256=descriptor.descriptor_sha256,
        input_schema_sha256=descriptor.input_schema_sha256,
        output_media_type=descriptor.output.media_type,
        maximum_output_bytes=descriptor.output.maximum_bytes,
    )
