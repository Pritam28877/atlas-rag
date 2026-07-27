"""Bounded hierarchy, provenance, and capability intersection tests."""

import hashlib
import json

import pytest

from app.services.harness.extensions import (
    DiscoveryError,
    DiscoveryLimits,
    InstructionCandidate,
    InstructionKind,
    InstructionScope,
    discover_instructions,
)


def _candidate(
    *,
    scope: InstructionScope,
    content: str,
    name: str = "review",
    kind: InstructionKind = InstructionKind.SKILL,
    path: str = "skills/review.md",
    capabilities: tuple[str, ...] = ("files.read", "network.fetch"),
    depth: int = 1,
) -> InstructionCandidate:
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    provenance = json.dumps(
        {
            "content_sha256": content_hash,
            "depth": depth,
            "kind": kind.value,
            "name": name,
            "relative_path": path,
            "scope": scope.value,
        },
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return InstructionCandidate(
        kind=kind,
        name=name,
        scope=scope,
        relative_path=path,
        depth=depth,
        content=content,
        declared_capabilities=capabilities,
        content_sha256=content_hash,
        provenance_sha256=hashlib.sha256(provenance.encode()).hexdigest(),
    )


def test_closest_scope_wins_and_capabilities_only_intersect() -> None:
    result = discover_instructions(
        (
            _candidate(scope=InstructionScope.GLOBAL, content="global"),
            _candidate(scope=InstructionScope.WORKSPACE, content="workspace"),
        ),
        granted_capabilities=("files.read",),
    )

    assert len(result.entries) == 1
    assert result.entries[0].content == "workspace"
    assert result.entries[0].effective_capabilities == ("files.read",)


def test_same_scope_collision_and_depth_are_rejected() -> None:
    with pytest.raises(DiscoveryError):
        discover_instructions(
            (
                _candidate(scope=InstructionScope.WORKSPACE, content="one"),
                _candidate(scope=InstructionScope.WORKSPACE, content="two"),
            ),
            granted_capabilities=(),
        )
    with pytest.raises(DiscoveryError):
        discover_instructions(
            (_candidate(scope=InstructionScope.GLOBAL, content="deep", depth=9),),
            granted_capabilities=(),
            limits=DiscoveryLimits(max_depth=8),
        )


def test_provenance_and_path_tampering_fail_before_selection() -> None:
    candidate = _candidate(scope=InstructionScope.GLOBAL, content="safe")
    with pytest.raises(ValueError):
        InstructionCandidate.model_validate(
            candidate.model_dump() | {"content": "tampered"}
        )
    with pytest.raises(ValueError):
        _candidate(
            scope=InstructionScope.GLOBAL,
            content="unsafe",
            path="../outside.md",
        )
