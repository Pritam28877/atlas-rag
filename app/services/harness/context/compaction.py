"""Safe compaction summaries and reconstruction validation."""

from __future__ import annotations

import hashlib
import json
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedLabel,
    BoundedReason,
    EventId,
    Sha256,
    StrictProtocolModel,
)
from app.services.harness.protocol.conversation import ItemKind, SourceId

MAXIMUM_COMPACTION_ITEMS = 512
MAXIMUM_SUMMARY_BYTES = 128 * 1024


class CompactionItem(StrictProtocolModel):
    source_id: SourceId
    event_id: EventId
    kind: ItemKind
    pair_id: SourceId | None = None
    content_sha256: Sha256
    token_count: int = Field(ge=0, le=2_000_000)
    critical_fact_ids: tuple[BoundedLabel, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def validate_facts(self) -> Self:
        if tuple(sorted(set(self.critical_fact_ids))) != self.critical_fact_ids:
            raise ValueError("compaction fact IDs must be unique and sorted")
        return self


class CompactionSourceHash(StrictProtocolModel):
    source_id: SourceId
    content_sha256: Sha256


class CompactionSummary(StrictProtocolModel):
    covered_event_start: int = Field(ge=0)
    covered_event_end: int = Field(ge=0)
    source_hashes: tuple[CompactionSourceHash, ...] = Field(
        max_length=MAXIMUM_COMPACTION_ITEMS
    )
    summary_text: str = Field(min_length=1, max_length=MAXIMUM_SUMMARY_BYTES)
    critical_fact_ids: tuple[BoundedLabel, ...] = Field(max_length=64)
    summarizer_revision: BoundedLabel
    summary_sha256: Sha256

    @model_validator(mode="after")
    def validate_range_and_hash(self) -> Self:
        if self.covered_event_end < self.covered_event_start:
            raise ValueError("compaction event range is inverted")
        if tuple(
            sorted(
                set(source.source_id for source in self.source_hashes)
            )
        ) != tuple(source.source_id for source in self.source_hashes):
            raise ValueError("compaction source hashes must be sorted and unique")
        if tuple(sorted(set(self.critical_fact_ids))) != self.critical_fact_ids:
            raise ValueError("summary fact IDs must be unique and sorted")
        if _summary_hash(self) != self.summary_sha256:
            raise ValueError("compaction summary hash is invalid")
        return self


class CompactionValidationResult(StrictProtocolModel):
    valid: bool
    missing_fact_ids: tuple[BoundedLabel, ...] = Field(max_length=64)
    restore_source_ids: tuple[SourceId, ...] = Field(
        max_length=MAXIMUM_COMPACTION_ITEMS
    )
    reason: BoundedReason | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.valid and (
            self.missing_fact_ids or self.restore_source_ids or self.reason
        ):
            raise ValueError("valid compaction cannot contain reconstruction failures")
        if not self.valid and self.reason is None:
            raise ValueError("invalid compaction requires a reason")
        return self


class CompactionError(ValueError):
    pass


def create_compaction_summary(
    items: tuple[CompactionItem, ...],
    *,
    covered_event_start: int,
    covered_event_end: int,
    summary_text: str,
    critical_fact_ids: tuple[BoundedLabel, ...],
    summarizer_revision: BoundedLabel,
) -> CompactionSummary:
    verified_items = _verify_items(items)
    source_hashes = tuple(
        CompactionSourceHash(
            source_id=item.source_id,
            content_sha256=item.content_sha256,
        )
        for item in sorted(verified_items, key=lambda value: value.source_id)
    )
    summary = CompactionSummary.model_construct(
        covered_event_start=covered_event_start,
        covered_event_end=covered_event_end,
        source_hashes=source_hashes,
        summary_text=summary_text,
        critical_fact_ids=tuple(sorted(set(critical_fact_ids))),
        summarizer_revision=summarizer_revision,
        summary_sha256="0" * 64,
    )
    return CompactionSummary.model_validate(
        summary.model_copy(update={"summary_sha256": _summary_hash(summary)})
    )


def validate_compaction(
    summary: CompactionSummary,
    items: tuple[CompactionItem, ...],
    *,
    required_fact_ids: tuple[BoundedLabel, ...],
) -> CompactionValidationResult:
    try:
        verified_summary = CompactionSummary.model_validate(summary.model_dump())
        verified_items = _verify_items(items)
    except Exception:
        return CompactionValidationResult(
            valid=False,
            missing_fact_ids=tuple(sorted(set(required_fact_ids))),
            restore_source_ids=(),
            reason="compaction evidence failed schema validation",
        )
    expected_hashes = {
        item.source_id: item.content_sha256 for item in verified_items
    }
    mismatched = tuple(
        source.source_id
        for source in verified_summary.source_hashes
        if expected_hashes.get(source.source_id) != source.content_sha256
    )
    missing_sources = tuple(
        source_id
        for source_id in expected_hashes
        if source_id
        not in {source.source_id for source in verified_summary.source_hashes}
    )
    facts = set(verified_summary.critical_fact_ids)
    missing_facts = tuple(sorted(set(required_fact_ids).difference(facts)))
    restore = tuple(sorted(set(mismatched).union(missing_sources)))
    if missing_facts or restore:
        return CompactionValidationResult(
            valid=False,
            missing_fact_ids=missing_facts,
            restore_source_ids=restore,
            reason="critical facts or source hashes require reconstruction",
        )
    return CompactionValidationResult(
        valid=True,
        missing_fact_ids=(),
        restore_source_ids=(),
    )


def _verify_items(items: tuple[CompactionItem, ...]) -> tuple[CompactionItem, ...]:
    if len(items) > MAXIMUM_COMPACTION_ITEMS:
        raise CompactionError("compaction item count exceeds limit")
    verified = tuple(CompactionItem.model_validate(item.model_dump()) for item in items)
    source_ids = tuple(item.source_id for item in verified)
    if len(source_ids) != len(set(source_ids)):
        raise CompactionError("compaction source IDs must be unique")
    pairs: dict[str, set[ItemKind]] = {}
    for item in verified:
        if item.pair_id is not None:
            pairs.setdefault(item.pair_id, set()).add(item.kind)
    for kinds in pairs.values():
        if kinds != {ItemKind.TOOL_CALL, ItemKind.TOOL_RESULT}:
            raise CompactionError("tool call and result pairs cannot be separated")
    return verified


def _summary_hash(summary: CompactionSummary) -> str:
    values = summary.model_dump(mode="json", exclude={"summary_sha256"})
    return hashlib.sha256(
        json.dumps(
            values,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
