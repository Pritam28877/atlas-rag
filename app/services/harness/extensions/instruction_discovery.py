"""Bounded, provenance-checked skill and instruction discovery."""

from __future__ import annotations

import hashlib
import json
from enum import IntEnum, StrEnum
from pathlib import PurePosixPath
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedLabel,
    Capability,
    Sha256,
    StrictProtocolModel,
)

DISCOVERY_MAX_ENTRIES = 256
DISCOVERY_MAX_DEPTH = 32
DISCOVERY_MAX_TOTAL_BYTES = 4 * 1024 * 1024
DISCOVERY_MAX_CONTENT_BYTES = 64 * 1024


class InstructionKind(StrEnum):
    SKILL = "skill"
    INSTRUCTION = "instruction"


class InstructionScope(IntEnum):
    GLOBAL = 0
    TENANT = 1
    WORKSPACE = 2
    SESSION = 3


class InstructionCandidate(StrictProtocolModel):
    """One trusted, pre-read candidate from an approved discovery source."""

    kind: InstructionKind
    name: BoundedLabel = Field(pattern=r"^[a-z][a-z0-9._-]*$")
    scope: InstructionScope
    relative_path: str = Field(min_length=1, max_length=1024)
    depth: int = Field(ge=0, le=DISCOVERY_MAX_DEPTH)
    content: str = Field(min_length=1, max_length=DISCOVERY_MAX_CONTENT_BYTES)
    declared_capabilities: tuple[Capability, ...] = Field(max_length=64)
    content_sha256: Sha256
    provenance_sha256: Sha256

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        path = PurePosixPath(self.relative_path)
        if (
            path.is_absolute()
            or path.as_posix() != self.relative_path
            or "." in path.parts
            or ".." in path.parts
        ):
            raise ValueError("instruction path must be canonical and relative")
        if tuple(sorted(set(self.declared_capabilities))) != self.declared_capabilities:
            raise ValueError("instruction capabilities must be unique and sorted")
        content_bytes = self.content.encode("utf-8")
        if len(content_bytes) > DISCOVERY_MAX_CONTENT_BYTES:
            raise ValueError("instruction content exceeds byte limit")
        if hashlib.sha256(content_bytes).hexdigest() != self.content_sha256:
            raise ValueError("instruction content hash is invalid")
        provenance = _provenance_hash(
            kind=self.kind,
            name=self.name,
            scope=self.scope,
            relative_path=self.relative_path,
            depth=self.depth,
            content_sha256=self.content_sha256,
        )
        if provenance != self.provenance_sha256:
            raise ValueError("instruction provenance hash is invalid")
        return self


class DiscoveryLimits(StrictProtocolModel):
    max_entries: int = Field(
        default=DISCOVERY_MAX_ENTRIES,
        ge=1,
        le=DISCOVERY_MAX_ENTRIES,
    )
    max_depth: int = Field(default=8, ge=0, le=DISCOVERY_MAX_DEPTH)
    max_total_bytes: int = Field(
        default=256 * 1024,
        ge=1,
        le=DISCOVERY_MAX_TOTAL_BYTES,
    )


class DiscoveredInstruction(StrictProtocolModel):
    kind: InstructionKind
    name: BoundedLabel
    scope: InstructionScope
    relative_path: str
    content: str
    content_sha256: Sha256
    provenance_sha256: Sha256
    effective_capabilities: tuple[Capability, ...] = Field(max_length=64)


class DiscoveryResult(StrictProtocolModel):
    entries: tuple[DiscoveredInstruction, ...] = Field(max_length=DISCOVERY_MAX_ENTRIES)
    total_bytes: int = Field(ge=0, le=DISCOVERY_MAX_TOTAL_BYTES)
    discovery_sha256: Sha256

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        identities = tuple((entry.kind.value, entry.name) for entry in self.entries)
        if identities != tuple(sorted(set(identities))):
            raise ValueError(
                "discovered instruction identities must be unique and sorted"
            )
        actual_total = sum(len(entry.content.encode("utf-8")) for entry in self.entries)
        if actual_total != self.total_bytes:
            raise ValueError("discovery byte total is invalid")
        expected = _discovery_hash(self.entries, self.total_bytes)
        if expected != self.discovery_sha256:
            raise ValueError("discovery hash is invalid")
        return self


class DiscoveryError(ValueError):
    pass


def discover_instructions(
    candidates: tuple[InstructionCandidate, ...],
    *,
    granted_capabilities: tuple[Capability, ...],
    limits: DiscoveryLimits | None = None,
) -> DiscoveryResult:
    """Select the closest candidate without expanding the current grant."""

    verified_limits = DiscoveryLimits.model_validate(
        (limits or DiscoveryLimits()).model_dump()
    )
    granted = tuple(sorted(set(granted_capabilities)))
    if granted != granted_capabilities:
        raise DiscoveryError("granted capabilities must be unique and sorted")
    if len(candidates) > verified_limits.max_entries:
        raise DiscoveryError("instruction candidate count exceeds limit")
    selected: dict[tuple[InstructionKind, str], InstructionCandidate] = {}
    total_bytes = 0
    for candidate in candidates:
        verified = InstructionCandidate.model_validate(candidate.model_dump())
        if verified.depth > verified_limits.max_depth:
            raise DiscoveryError("instruction depth exceeds limit")
        total_bytes += len(verified.content.encode("utf-8"))
        if total_bytes > verified_limits.max_total_bytes:
            raise DiscoveryError("instruction bytes exceed limit")
        identity = (verified.kind, verified.name)
        previous = selected.get(identity)
        if previous is not None and previous.scope == verified.scope:
            raise DiscoveryError("same-scope instruction collision")
        if previous is None or verified.scope > previous.scope:
            selected[identity] = verified
    entries = tuple(
        DiscoveredInstruction(
            kind=candidate.kind,
            name=candidate.name,
            scope=candidate.scope,
            relative_path=candidate.relative_path,
            content=candidate.content,
            content_sha256=candidate.content_sha256,
            provenance_sha256=candidate.provenance_sha256,
            effective_capabilities=tuple(
                capability
                for capability in candidate.declared_capabilities
                if capability in granted
            ),
        )
        for candidate in sorted(
            selected.values(),
            key=lambda value: (value.kind.value, value.name),
        )
    )
    output_bytes = sum(len(entry.content.encode("utf-8")) for entry in entries)
    return DiscoveryResult(
        entries=entries,
        total_bytes=output_bytes,
        discovery_sha256=_discovery_hash(entries, output_bytes),
    )


def instruction_provenance_sha256(candidate: InstructionCandidate) -> str:
    return _provenance_hash(
        kind=candidate.kind,
        name=candidate.name,
        scope=candidate.scope,
        relative_path=candidate.relative_path,
        depth=candidate.depth,
        content_sha256=candidate.content_sha256,
    )


def _provenance_hash(**values: object) -> str:
    return hashlib.sha256(
        json.dumps(
            values,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            default=lambda value: value.value
            if isinstance(value, (InstructionKind, InstructionScope))
            else value,
        ).encode("utf-8")
    ).hexdigest()


def _discovery_hash(
    entries: tuple[DiscoveredInstruction, ...],
    total_bytes: int,
) -> str:
    payload = {
        "entries": [entry.model_dump(mode="json") for entry in entries],
        "total_bytes": total_bytes,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
