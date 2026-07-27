"""Load checksum-pinned packaged conformance recordings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, cast

from app.services.harness.providers.conformance_contracts import (
    ConformanceScenario,
)
from app.services.harness.providers.conformance_recorded import (
    RecordedConformanceCase,
    RecordedConformanceCaseMode,
    recorded_records_sha256,
)
from app.services.harness.providers.conformance_resources import (
    CONFORMANCE_FIXTURE_ROOT,
)

type ConformanceFixtureGroup = Literal[
    "bedrock",
    "mock",
    "openai_family",
    "vertex",
]


def load_recorded_conformance_cases(
    fixture_group: ConformanceFixtureGroup,
) -> tuple[RecordedConformanceCase, ...]:
    root = CONFORMANCE_FIXTURE_ROOT / fixture_group
    manifest = _manifest(root / "manifest.json")
    return (
        _case(
            root,
            manifest,
            ConformanceScenario.CANCELLATION,
            RecordedConformanceCaseMode.CANCEL,
        ),
        _case(
            root,
            manifest,
            ConformanceScenario.MALFORMED_STREAM,
            RecordedConformanceCaseMode.EXPECT_DECODE_ERROR,
            "malformed.jsonl",
        ),
        _case(
            root,
            manifest,
            ConformanceScenario.OUTPUT_LIMIT,
            RecordedConformanceCaseMode.DECODE,
            "output_limit.jsonl",
        ),
        _case(
            root,
            manifest,
            ConformanceScenario.POLICY,
            RecordedConformanceCaseMode.DECODE,
            "policy.jsonl",
        ),
        _case(
            root,
            manifest,
            ConformanceScenario.REASONING_STREAM,
            RecordedConformanceCaseMode.DECODE,
            "reasoning.jsonl",
        ),
        _case(
            root,
            manifest,
            ConformanceScenario.TEXT_STREAM,
            RecordedConformanceCaseMode.DECODE,
            "text.jsonl",
        ),
        _case(
            root,
            manifest,
            ConformanceScenario.TOOL_CALLS,
            RecordedConformanceCaseMode.DECODE,
            "tools.jsonl",
        ),
        _case(
            root,
            manifest,
            ConformanceScenario.USAGE_COST,
            RecordedConformanceCaseMode.DECODE,
            "text.jsonl",
        ),
    )


def _case(
    root: Path,
    manifest: dict[str, str],
    scenario: ConformanceScenario,
    mode: RecordedConformanceCaseMode,
    fixture_name: str | None = None,
) -> RecordedConformanceCase:
    records = (
        ()
        if fixture_name is None
        else tuple((root / fixture_name).read_bytes().splitlines())
    )
    expected_digest = (
        recorded_records_sha256(records)
        if fixture_name is None
        else manifest[fixture_name]
    )
    return RecordedConformanceCase(
        scenario=scenario,
        mode=mode,
        records=records,
        records_sha256=expected_digest,
    )


def _manifest(path: Path) -> dict[str, str]:
    value = json.loads(path.read_bytes())
    if (
        not isinstance(value, dict)
        or any(
            not isinstance(name, str) or not isinstance(digest, str)
            for name, digest in value.items()
        )
    ):
        raise ValueError("conformance fixture manifest is invalid")
    return cast(dict[str, str], value)
