"""Context manifest, budget, compaction, and reconstruction tests."""

import hashlib
from datetime import UTC, datetime

import pytest

from app.services.harness.context import (
    CompactionError,
    CompactionItem,
    ContextBudgetError,
    ContextCandidate,
    ContextCompiler,
    ContextCompileRequest,
    create_compaction_summary,
    validate_compaction,
)
from app.services.harness.protocol import (
    ContextSourceKind,
    ContextSourceReference,
    ItemKind,
)

UTC = UTC
POLICY = "pol_" + "1" * 64
CONTEXT_ID = "ctx_" + "1" * 32
TURN_ID = "trn_" + "2" * 32


def _source(source_id: str, tokens: int, priority: int) -> ContextSourceReference:
    return ContextSourceReference(
        source_id=source_id,
        kind=ContextSourceKind.THREAD_TAIL,
        content_sha256=hashlib.sha256(source_id.encode()).hexdigest(),
        token_count=tokens,
        priority=priority,
    )


def _request(candidates: tuple[ContextCandidate, ...]) -> ContextCompileRequest:
    return ContextCompileRequest(
        context_id=CONTEXT_ID,
        turn_id=TURN_ID,
        provider_policy_version=POLICY,
        context_window_tokens=120,
        reserved_output_tokens=20,
        reserved_reasoning_tokens=10,
        reserved_tool_tokens=10,
        candidates=candidates,
        compiled_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_context_compiler_is_deterministic_and_omits_low_priority_sources() -> None:
    candidates = (
        ContextCandidate(reference=_source("tail", 40, 1)),
        ContextCandidate(reference=_source("policy", 30, 10), required=True),
        ContextCandidate(reference=_source("memory", 60, 2)),
    )
    first = ContextCompiler().compile(_request(candidates))
    second = ContextCompiler().compile(_request(tuple(reversed(candidates))))

    assert first.manifest_sha256 == second.manifest_sha256
    assert tuple(source.source_id for source in first.manifest.sources) == (
        "policy",
        "tail",
    )
    assert first.manifest.omissions[0].source_id == "memory"


def test_required_context_that_does_not_fit_fails_closed() -> None:
    candidate = ContextCandidate(reference=_source("required", 81, 1), required=True)
    with pytest.raises(ContextBudgetError):
        ContextCompiler().compile(_request((candidate,)))


def _item(
    source_id: str,
    kind: ItemKind,
    *,
    pair_id: str | None = None,
) -> CompactionItem:
    return CompactionItem(
        source_id=source_id,
        event_id="evt_" + hashlib.sha256(source_id.encode()).hexdigest()[:32],
        kind=kind,
        pair_id=pair_id,
        content_sha256=hashlib.sha256(source_id.encode()).hexdigest(),
        token_count=2,
    )


def test_compaction_preserves_pairs_and_validates_critical_facts() -> None:
    items = (
        _item("user", ItemKind.USER),
        _item("call", ItemKind.TOOL_CALL, pair_id="toolpair"),
        _item("result", ItemKind.TOOL_RESULT, pair_id="toolpair"),
    )
    summary = create_compaction_summary(
        items,
        covered_event_start=1,
        covered_event_end=3,
        summary_text="checked tool result",
        critical_fact_ids=(),
        summarizer_revision="summary.v1",
    )
    validation = validate_compaction(summary, items, required_fact_ids=())
    assert validation.valid

    with pytest.raises(CompactionError):
        create_compaction_summary(
            (items[1],),
            covered_event_start=1,
            covered_event_end=1,
            summary_text="broken",
            critical_fact_ids=(),
            summarizer_revision="summary.v1",
        )


def test_compaction_detects_tampered_source_hash() -> None:
    item = _item("source", ItemKind.USER)
    summary = create_compaction_summary(
        (item,),
        covered_event_start=1,
        covered_event_end=1,
        summary_text="summary",
        critical_fact_ids=("fact.required",),
        summarizer_revision="summary.v1",
    )
    tampered = item.model_copy(
        update={"content_sha256": hashlib.sha256(b"tampered").hexdigest()}
    )
    validation = validate_compaction(
        summary,
        (tampered,),
        required_fact_ids=("fact.required",),
    )
    assert not validation.valid
    assert validation.restore_source_ids == ("source",)
