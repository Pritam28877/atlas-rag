"""Deterministic non-retaining DLP inspection for provider request bodies."""

from __future__ import annotations

import hashlib
import re
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol import Sha256, StrictProtocolModel
from app.services.harness.providers.egress_contracts import (
    PayloadInspection,
    ProviderEgressRequest,
)

MAXIMUM_DLP_DENIED_DIGESTS = 256
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(rb'"(?:api[_-]?key|authorization)"\s*:', re.IGNORECASE),
)


class ProviderPayloadInspectionPolicy(StrictProtocolModel):
    policy_revision_sha256: Sha256
    denied_body_sha256s: tuple[Sha256, ...] = Field(
        max_length=MAXIMUM_DLP_DENIED_DIGESTS
    )
    maximum_scan_bytes: int = Field(
        ge=1,
        le=16 * 1024 * 1024,
    )

    @model_validator(mode="after")
    def validate_denied_digests(self) -> Self:
        denied = self.denied_body_sha256s
        if tuple(sorted(set(denied))) != denied:
            raise ValueError("DLP denied body hashes must be unique and sorted")
        return self


class DeterministicProviderPayloadInspector:
    """Returns only policy evidence and never stores request body bytes."""

    def __init__(self, policy: ProviderPayloadInspectionPolicy) -> None:
        self._policy = policy

    async def inspect(
        self,
        request: ProviderEgressRequest,
    ) -> PayloadInspection:
        body = request.body()
        if len(body) > self._policy.maximum_scan_bytes:
            return self._decision(
                allowed=False,
                reason="Provider payload exceeds the DLP scan bound.",
            )
        body_sha256 = hashlib.sha256(body).hexdigest()
        if body_sha256 in self._policy.denied_body_sha256s:
            return self._decision(
                allowed=False,
                reason="Provider payload matches denied DLP evidence.",
            )
        if any(pattern.search(body) is not None for pattern in SECRET_PATTERNS):
            return self._decision(
                allowed=False,
                reason="Provider payload contains credential-like material.",
            )
        return self._decision(
            allowed=True,
            reason="Provider payload passed deterministic DLP inspection.",
        )

    def _decision(self, *, allowed: bool, reason: str) -> PayloadInspection:
        return PayloadInspection(
            allowed=allowed,
            policy_revision_sha256=self._policy.policy_revision_sha256,
            reason=reason,
        )
