"""Pinned task and security fixture catalog with explicit coverage checks."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol import Sha256, StrictProtocolModel
from app.services.harness.protocol.base import BoundedLabel

MAXIMUM_FIXTURES = 1_000
MAXIMUM_FIXTURE_TAGS = 16


class FixtureKind(StrEnum):
    NAVIGATION = "navigation"
    BUG = "bug"
    FEATURE = "feature"
    REFACTOR = "refactor"
    TEST_REPAIR = "test_repair"
    REFUSAL = "refusal"
    INJECTION = "injection"
    MALICIOUS_OUTPUT = "malicious_output"
    CRASH = "crash"
    CANCELLATION = "cancellation"
    PRIVATE_EGRESS = "private_egress"
    MULTI_AGENT_CONFLICT = "multi_agent_conflict"


REQUIRED_FIXTURE_KINDS = tuple(sorted(FixtureKind, key=lambda kind: kind.value))


class EvaluationFixture(StrictProtocolModel):
    fixture_sha256: Sha256
    kind: FixtureKind
    title: BoundedLabel
    scenario_sha256: Sha256
    acceptance_sha256: Sha256
    expected_outcome: BoundedLabel
    tags: tuple[BoundedLabel, ...] = Field(max_length=MAXIMUM_FIXTURE_TAGS)

    @model_validator(mode="after")
    def validate_fixture(self) -> Self:
        if tuple(sorted(set(self.tags))) != self.tags:
            raise ValueError("fixture tags must be unique and sorted")
        expected = fixture_sha256(self.model_dump(mode="json"))
        if expected != self.fixture_sha256:
            raise ValueError("fixture hash is invalid")
        return self


class FixtureCoverage(StrictProtocolModel):
    total: int = Field(ge=0, le=MAXIMUM_FIXTURES)
    covered_kinds: tuple[FixtureKind, ...] = Field(max_length=len(FixtureKind))
    missing_kinds: tuple[FixtureKind, ...] = Field(max_length=len(FixtureKind))

    @model_validator(mode="after")
    def validate_partition(self) -> Self:
        if set(self.covered_kinds) & set(self.missing_kinds):
            raise ValueError("fixture coverage sets must be disjoint")
        if set(self.covered_kinds) | set(self.missing_kinds) != set(FixtureKind):
            raise ValueError("fixture coverage must account for every kind")
        return self


class FixtureRegistry:
    """Bounded immutable-by-identity fixture registry."""

    def __init__(self, *, maximum_fixtures: int = MAXIMUM_FIXTURES) -> None:
        if not 1 <= maximum_fixtures <= MAXIMUM_FIXTURES:
            raise ValueError("fixture capacity is outside the configured bound")
        self._maximum_fixtures = maximum_fixtures
        self._fixtures: dict[str, EvaluationFixture] = {}

    def register(self, fixture: EvaluationFixture) -> EvaluationFixture:
        existing = self._fixtures.get(fixture.fixture_sha256)
        if existing is not None and existing != fixture:
            raise ValueError("fixture identity collision")
        if existing is None and len(self._fixtures) >= self._maximum_fixtures:
            raise ValueError("fixture capacity is exhausted")
        self._fixtures[fixture.fixture_sha256] = fixture
        return fixture

    def coverage(self) -> FixtureCoverage:
        covered = tuple(
            sorted(
                {fixture.kind for fixture in self._fixtures.values()},
                key=lambda kind: kind.value,
            )
        )
        missing = tuple(kind for kind in REQUIRED_FIXTURE_KINDS if kind not in covered)
        return FixtureCoverage(
            total=len(self._fixtures),
            covered_kinds=covered,
            missing_kinds=missing,
        )

    def require_complete(self) -> FixtureCoverage:
        coverage = self.coverage()
        if coverage.missing_kinds:
            missing = ", ".join(kind.value for kind in coverage.missing_kinds)
            raise ValueError(f"fixture suite is incomplete: {missing}")
        return coverage

    def records(self) -> tuple[EvaluationFixture, ...]:
        return tuple(self._fixtures[key] for key in sorted(self._fixtures))


def build_fixture(
    *,
    kind: FixtureKind,
    title: str,
    scenario_sha256: Sha256,
    acceptance_sha256: Sha256,
    expected_outcome: str,
    tags: tuple[str, ...] = (),
) -> EvaluationFixture:
    provisional = EvaluationFixture.model_construct(
        fixture_sha256="0" * 64,
        kind=kind,
        title=title,
        scenario_sha256=scenario_sha256,
        acceptance_sha256=acceptance_sha256,
        expected_outcome=expected_outcome,
        tags=tags,
    )
    return EvaluationFixture(
        fixture_sha256=fixture_sha256(
            provisional.model_dump(mode="json", warnings=False)
        ),
        kind=kind,
        title=title,
        scenario_sha256=scenario_sha256,
        acceptance_sha256=acceptance_sha256,
        expected_outcome=expected_outcome,
        tags=tags,
    )


def fixture_sha256(values: object) -> str:
    if isinstance(values, dict):
        values = {
            key: value
            for key, value in values.items()
            if key != "fixture_sha256"
        }
    return hashlib.sha256(
        json.dumps(
            values,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
