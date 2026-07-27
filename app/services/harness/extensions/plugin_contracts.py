"""Bounded call/result contracts for out-of-process plugins."""

from __future__ import annotations

import hashlib
import json
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    Capability,
    RequestId,
    Sha256,
    StrictProtocolModel,
)
from app.services.harness.protocol.base import BoundedLabel

PLUGIN_MAX_PENDING_CALLS = 16
PLUGIN_HARD_PENDING_CALLS = 64
PLUGIN_MAX_PAYLOAD_BYTES = 512 * 1024


class PluginDescriptor(StrictProtocolModel):
    name: BoundedLabel
    version: BoundedLabel
    capabilities: tuple[Capability, ...] = Field(max_length=64)
    max_pending_calls: int = Field(
        default=PLUGIN_MAX_PENDING_CALLS,
        ge=1,
        le=PLUGIN_HARD_PENDING_CALLS,
    )

    @model_validator(mode="after")
    def validate_capabilities(self) -> Self:
        if tuple(sorted(set(self.capabilities))) != self.capabilities:
            raise ValueError("plugin capabilities must be unique and sorted")
        return self


class PluginCall(StrictProtocolModel):
    request_id: RequestId
    method: BoundedLabel
    capability: Capability
    arguments_json: str = Field(min_length=2, max_length=PLUGIN_MAX_PAYLOAD_BYTES)
    timeout_ms: int = Field(default=60_000, ge=100, le=3_600_000)

    @model_validator(mode="after")
    def validate_arguments(self) -> Self:
        _canonical_object(self.arguments_json, "plugin arguments")
        return self


class PluginResult(StrictProtocolModel):
    request_id: RequestId
    output_json: str = Field(min_length=2, max_length=PLUGIN_MAX_PAYLOAD_BYTES)
    output_bytes: int = Field(ge=1, le=PLUGIN_MAX_PAYLOAD_BYTES)
    output_sha256: Sha256

    @model_validator(mode="after")
    def validate_output(self) -> Self:
        _canonical_object(self.output_json, "plugin output")
        encoded = self.output_json.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        if len(encoded) != self.output_bytes or digest != self.output_sha256:
            raise ValueError("plugin output metadata is invalid")
        return self


def _canonical_object(value: str, label: str) -> None:
    try:
        parsed = json.loads(value)
        canonical = json.dumps(
            parsed,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be valid JSON") from error
    if not isinstance(parsed, dict) or canonical != value:
        raise ValueError(f"{label} must be a canonical JSON object")
