"""Pinned fixture catalog and coverage tests."""

import hashlib

import pytest

from app.services.harness.evaluation import (
    REQUIRED_FIXTURE_KINDS,
    FixtureKind,
    FixtureRegistry,
    build_fixture,
)


def _fixture(kind: FixtureKind):
    marker = hashlib.sha256(kind.value.encode()).hexdigest()
    return build_fixture(
        kind=kind,
        title=f"fixture {kind.value}",
        scenario_sha256=marker,
        acceptance_sha256=marker,
        expected_outcome="pass",
        tags=(kind.value,),
    )


def test_required_fixture_matrix_is_explicit_and_bounded() -> None:
    registry = FixtureRegistry()
    registry.register(_fixture(FixtureKind.NAVIGATION))
    coverage = registry.coverage()
    assert coverage.total == 1
    assert FixtureKind.NAVIGATION in coverage.covered_kinds
    assert len(coverage.missing_kinds) == len(REQUIRED_FIXTURE_KINDS) - 1
    with pytest.raises(ValueError, match="incomplete"):
        registry.require_complete()


def test_complete_fixture_matrix_can_be_required() -> None:
    registry = FixtureRegistry()
    for kind in REQUIRED_FIXTURE_KINDS:
        registry.register(_fixture(kind))
    coverage = registry.require_complete()
    assert coverage.missing_kinds == ()
