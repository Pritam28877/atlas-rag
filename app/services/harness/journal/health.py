"""Stable journal verification and operator-health contracts."""

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.journal.projection_store import FailureCode
from app.services.harness.protocol import StrictProtocolModel, UtcTimestamp

MAXIMUM_VERIFICATION_RECORDS = 1_000_000


class JournalHealthStatus(StrEnum):
    HEALTHY = "healthy"
    NEEDS_OPERATOR = "needs_operator"


class JournalCorruptionCode(StrEnum):
    EVENT_CORRUPT = "event_corrupt"
    RECEIPT_CORRUPT = "receipt_corrupt"
    PROJECTION_CORRUPT = "projection_corrupt"
    SEQUENCE_CORRUPT = "sequence_corrupt"
    SQLITE_QUICK_CHECK = "sqlite_quick_check"
    NEEDS_OPERATOR = "needs_operator"


class JournalCorruptionError(RuntimeError):
    def __init__(self, code: JournalCorruptionCode) -> None:
        super().__init__("journal integrity verification failed")
        self.code = code


class JournalHealthRecord(StrictProtocolModel):
    generation: int = Field(ge=1, le=2**63 - 1)
    status: JournalHealthStatus
    failure_code: FailureCode | None = None
    verified_event_count: int = Field(ge=0, le=2**63 - 1)
    verified_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_health(self) -> Self:
        if (self.status is JournalHealthStatus.HEALTHY) != (
            self.failure_code is None
        ):
            raise ValueError("journal health and failure code disagree")
        return self


class JournalVerificationResult(StrictProtocolModel):
    complete: bool
    events_checked: int = Field(ge=0, le=MAXIMUM_VERIFICATION_RECORDS)
    receipts_checked: int = Field(ge=0, le=MAXIMUM_VERIFICATION_RECORDS)
    projections_checked: int = Field(ge=0, le=MAXIMUM_VERIFICATION_RECORDS)
    verified_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def validate_completion(self) -> Self:
        checked_records = (
            self.events_checked
            + self.receipts_checked
            + self.projections_checked
        )
        if checked_records > MAXIMUM_VERIFICATION_RECORDS:
            raise ValueError("journal verification record limit exceeded")
        if self.complete != (self.verified_at is not None):
            raise ValueError("journal verification completion is inconsistent")
        return self
