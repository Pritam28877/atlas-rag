"""Fail-closed schema writer gates and rollback planning."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedReason,
    SchemaVersion,
    StrictProtocolModel,
)
from app.services.harness.protocol.compatibility import (
    CompatibilityAction,
    CompatibilityError,
    PreservedUnknownRecord,
    preserve_unknown_record,
    require_writer_rollback_compatibility,
)

MAXIMUM_ROLLBACK_READERS = 3
MAXIMUM_PRESERVED_UNKNOWN_RECORDS = 10_000


class WriterGateDecision(StrictProtocolModel):
    """Decision that must pass before a schema-changing writer starts."""

    writer_schema_version: SchemaVersion
    rollback_reader_versions: tuple[SchemaVersion, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_ROLLBACK_READERS,
    )
    actions: tuple[CompatibilityAction, ...] = Field(
        max_length=MAXIMUM_ROLLBACK_READERS,
    )
    allowed: bool
    reason: BoundedReason | None = None

    @model_validator(mode="after")
    def validate_gate(self) -> Self:
        if self.allowed:
            if not self.actions or CompatibilityAction.REJECT in self.actions:
                raise ValueError("allowed writer gate contains a rejected reader")
            if self.reason is not None:
                raise ValueError("allowed writer gate cannot include a reason")
            expected = require_writer_rollback_compatibility(
                self.writer_schema_version,
                self.rollback_reader_versions,
            )
            if self.actions != expected:
                raise ValueError("writer gate actions do not match compatibility")
        else:
            if self.reason is None:
                raise ValueError("blocked writer gate requires a reason")
            if self.actions and CompatibilityAction.REJECT not in self.actions:
                raise ValueError("blocked writer gate must expose rejection")
        return self


class UpgradePlan(StrictProtocolModel):
    """Immutable upgrade decision; journal rewrites are never permitted."""

    current_schema_version: SchemaVersion
    target_schema_version: SchemaVersion
    writer_gate: WriterGateDecision
    preserved_unknown_records: int = Field(
        ge=0,
        le=MAXIMUM_PRESERVED_UNKNOWN_RECORDS,
    )
    journal_rewrite_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_target_gate(self) -> Self:
        if self.writer_gate.writer_schema_version != self.target_schema_version:
            raise ValueError("writer gate must target the upgrade schema")
        return self


def plan_writer_gate(
    writer_schema_version: SchemaVersion,
    rollback_reader_versions: tuple[SchemaVersion, ...],
) -> WriterGateDecision:
    """Return a durable gate decision instead of silently allowing a reject."""

    try:
        actions = require_writer_rollback_compatibility(
            writer_schema_version,
            rollback_reader_versions,
        )
    except CompatibilityError as error:
        rejected_actions = tuple(
            CompatibilityAction.REJECT for _ in rollback_reader_versions
        )
        return WriterGateDecision(
            writer_schema_version=writer_schema_version,
            rollback_reader_versions=rollback_reader_versions,
            actions=rejected_actions,
            allowed=False,
            reason=str(error),
        )
    return WriterGateDecision(
        writer_schema_version=writer_schema_version,
        rollback_reader_versions=rollback_reader_versions,
        actions=actions,
        allowed=True,
    )


def plan_upgrade(
    *,
    current_schema_version: SchemaVersion,
    target_schema_version: SchemaVersion,
    rollback_reader_versions: tuple[SchemaVersion, ...],
    preserved_unknown_records: int = 0,
) -> UpgradePlan:
    """Build an upgrade plan with an explicit, immutable writer gate."""

    return UpgradePlan(
        current_schema_version=current_schema_version,
        target_schema_version=target_schema_version,
        writer_gate=plan_writer_gate(
            target_schema_version,
            rollback_reader_versions,
        ),
        preserved_unknown_records=preserved_unknown_records,
    )


def preserve_rollback_record(
    raw_json: bytes,
    reader_schema_version: SchemaVersion,
) -> PreservedUnknownRecord:
    """Preserve an unknown record exactly; incompatible records are rejected."""

    return preserve_unknown_record(raw_json, reader_schema_version)
