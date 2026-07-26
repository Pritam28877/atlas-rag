"""Durable, secret-free provider egress audit evidence."""

from enum import StrEnum

from pydantic import Field

from app.services.harness.protocol.base import (
    BoundedReason,
    RequestId,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.conversation import DataClassification
from app.services.harness.protocol.routing import ProviderName


class EgressAuditOutcome(StrEnum):
    AUTHORIZED = "authorized"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


class ProviderEgressAuditRecord(StrictProtocolModel):
    request_id: RequestId
    provider: ProviderName
    destination_sha256: Sha256
    target_url_sha256: Sha256
    classification: DataClassification
    body_sha256: Sha256
    inspection_policy_revision_sha256: Sha256
    body_bytes: int = Field(ge=1, le=16 * 1024 * 1024)
    attempt: int = Field(ge=1, le=64)
    redirect_count: int = Field(ge=0, le=5)
    outcome: EgressAuditOutcome
    response_status: int | None = Field(default=None, ge=100, le=599)
    response_bytes: int | None = Field(
        default=None,
        ge=0,
        le=16 * 1024 * 1024,
    )
    reason: BoundedReason
    recorded_at: UtcTimestamp
