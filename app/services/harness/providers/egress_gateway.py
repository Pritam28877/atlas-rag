"""Single audited provider egress coordinator."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import urlsplit

from app.services.harness.protocol import (
    CredentialSource,
    EgressAuditOutcome,
    ProviderEgressAuditRecord,
)
from app.services.harness.providers.credential_material import CredentialLease
from app.services.harness.providers.egress_contracts import (
    ProviderAddressResolver,
    ProviderEgressAuditSink,
    ProviderEgressConnector,
    ProviderEgressRequest,
    ProviderEgressResponse,
    ProviderPayloadInspector,
)
from app.services.harness.providers.egress_policy import (
    ProviderEgressPolicy,
    authorize_egress_target,
    canonical_provider_origin,
)

REDIRECT_STATUSES = frozenset({307, 308})


class ProviderEgressGatewayErrorCode(StrEnum):
    AUDIT = "audit"
    CANCELLED = "cancelled"
    CONNECTOR = "connector"
    DEADLINE = "deadline"
    INSPECTION = "inspection"
    PROVIDER = "provider"
    RESOLUTION = "resolution"
    RESPONSE_SIZE = "response_size"


class ProviderEgressGatewayError(RuntimeError):
    def __init__(self, code: ProviderEgressGatewayErrorCode) -> None:
        super().__init__("provider egress gateway rejected the operation")
        self.code = code


class ProviderEgressGateway:
    def __init__(
        self,
        *,
        credentials: CredentialSource[CredentialLease],
        resolver: ProviderAddressResolver,
        inspector: ProviderPayloadInspector,
        connector: ProviderEgressConnector,
        audit: ProviderEgressAuditSink,
        clock: Callable[[], datetime],
    ) -> None:
        self._credentials = credentials
        self._resolver = resolver
        self._inspector = inspector
        self._connector = connector
        self._audit = audit
        self._clock = clock

    async def send(
        self,
        request: ProviderEgressRequest,
        policy: ProviderEgressPolicy,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
        attempt: int,
    ) -> ProviderEgressResponse:
        self._check_runtime(cancellation, deadline_at)
        if request.metadata.provider != policy.provider:
            raise ProviderEgressGatewayError(
                ProviderEgressGatewayErrorCode.PROVIDER
            )
        try:
            inspection = await self._inspector.inspect(request)
        except Exception:
            raise ProviderEgressGatewayError(
                ProviderEgressGatewayErrorCode.INSPECTION
            ) from None
        if not inspection.allowed:
            raise ProviderEgressGatewayError(
                ProviderEgressGatewayErrorCode.INSPECTION
            )

        credential: CredentialLease | None = None
        target_url = request.metadata.target_url
        redirect_count = 0
        try:
            while True:
                self._check_runtime(cancellation, deadline_at)
                preliminary_target = authorize_egress_target(
                    policy,
                    target_url=target_url,
                    classification=request.metadata.classification,
                    request_bytes=request.metadata.body_bytes,
                    resolved_addresses=("1.1.1.1",),
                    redirect_count=redirect_count,
                )
                canonical_target_url = preliminary_target.canonical_url
                try:
                    addresses = await self._resolver.resolve(
                        preliminary_target.hostname,
                        preliminary_target.port,
                        cancellation=cancellation,
                        deadline_at=deadline_at,
                    )
                except Exception:
                    raise ProviderEgressGatewayError(
                        ProviderEgressGatewayErrorCode.RESOLUTION
                    ) from None
                target = authorize_egress_target(
                    policy,
                    target_url=canonical_target_url,
                    classification=request.metadata.classification,
                    request_bytes=request.metadata.body_bytes,
                    resolved_addresses=addresses,
                    redirect_count=redirect_count,
                )
                if credential is None:
                    credential = await self._credentials.acquire(
                        request.metadata.credential_handle,
                        provider=request.metadata.provider,
                        destination_sha256=policy.destination_sha256,
                        cancellation=cancellation,
                        deadline_at=deadline_at,
                    )
                await self._audit_record(
                    request,
                    canonical_target_url,
                    policy,
                    attempt=attempt,
                    redirect_count=redirect_count,
                    outcome=EgressAuditOutcome.AUTHORIZED,
                    reason="Provider egress attempt authorized.",
                    inspection_policy_revision_sha256=(
                        inspection.policy_revision_sha256
                    ),
                )
                connector_credential = (
                    credential
                    if _same_origin(
                        canonical_target_url,
                        policy.destination_url,
                    )
                    else None
                )
                try:
                    response = await self._connector.send(
                        request,
                        target,
                        connector_credential,
                        cancellation=cancellation,
                        deadline_at=deadline_at,
                        max_response_bytes=policy.max_response_bytes,
                    )
                except Exception:
                    await self._audit_record(
                        request,
                        canonical_target_url,
                        policy,
                        attempt=attempt,
                        redirect_count=redirect_count,
                        outcome=EgressAuditOutcome.FAILED,
                        reason="Provider connector failed.",
                        inspection_policy_revision_sha256=(
                            inspection.policy_revision_sha256
                        ),
                    )
                    raise ProviderEgressGatewayError(
                        ProviderEgressGatewayErrorCode.CONNECTOR
                    ) from None
                self._check_runtime(cancellation, deadline_at)
                if len(response.body()) > policy.max_response_bytes:
                    await self._audit_record(
                        request,
                        canonical_target_url,
                        policy,
                        attempt=attempt,
                        redirect_count=redirect_count,
                        outcome=EgressAuditOutcome.FAILED,
                        reason="Provider response exceeded the byte limit.",
                        inspection_policy_revision_sha256=(
                            inspection.policy_revision_sha256
                        ),
                    )
                    raise ProviderEgressGatewayError(
                        ProviderEgressGatewayErrorCode.RESPONSE_SIZE
                    )
                await self._audit_record(
                    request,
                    canonical_target_url,
                    policy,
                    attempt=attempt,
                    redirect_count=redirect_count,
                    outcome=EgressAuditOutcome.SUCCEEDED,
                    reason="Provider egress attempt completed.",
                    inspection_policy_revision_sha256=(
                        inspection.policy_revision_sha256
                    ),
                    response=response,
                )
                if (
                    response.status not in REDIRECT_STATUSES
                    or response.redirect_url is None
                ):
                    return response
                redirect_count += 1
                target_url = response.redirect_url
        finally:
            if credential is not None:
                await _release_credential(self._credentials, credential)

    def _check_runtime(
        self,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> None:
        now = self._clock()
        if (
            now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or deadline_at.tzinfo is None
            or deadline_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("provider egress times must use UTC")
        if cancellation.is_set():
            raise ProviderEgressGatewayError(
                ProviderEgressGatewayErrorCode.CANCELLED
            )
        if now >= deadline_at:
            raise ProviderEgressGatewayError(
                ProviderEgressGatewayErrorCode.DEADLINE
            )

    async def _audit_record(
        self,
        request: ProviderEgressRequest,
        target_url: str,
        policy: ProviderEgressPolicy,
        *,
        attempt: int,
        redirect_count: int,
        outcome: EgressAuditOutcome,
        reason: str,
        inspection_policy_revision_sha256: str,
        response: ProviderEgressResponse | None = None,
    ) -> None:
        try:
            await self._audit.record(
                ProviderEgressAuditRecord(
                    request_id=request.metadata.request_id,
                    provider=request.metadata.provider,
                    destination_sha256=policy.destination_sha256,
                    target_url_sha256=hashlib.sha256(
                        target_url.encode()
                    ).hexdigest(),
                    classification=request.metadata.classification,
                    body_sha256=request.metadata.body_sha256,
                    inspection_policy_revision_sha256=(
                        inspection_policy_revision_sha256
                    ),
                    body_bytes=request.metadata.body_bytes,
                    attempt=attempt,
                    redirect_count=redirect_count,
                    outcome=outcome,
                    response_status=(
                        response.status if response is not None else None
                    ),
                    response_bytes=(
                        len(response.body())
                        if response is not None
                        else None
                    ),
                    reason=reason,
                    recorded_at=self._clock(),
                )
            )
        except Exception:
            raise ProviderEgressGatewayError(
                ProviderEgressGatewayErrorCode.AUDIT
            ) from None


def _same_origin(first_url: str, second_url: str) -> bool:
    return canonical_provider_origin(
        _origin_url(first_url)
    ) == canonical_provider_origin(_origin_url(second_url))


def _origin_url(value: str) -> str:
    parts = urlsplit(value)
    port = parts.port
    netloc = parts.hostname or ""
    if port not in {None, 443}:
        netloc = f"{netloc}:{port}"
    return f"https://{netloc}"


async def _release_credential(
    credentials: CredentialSource[CredentialLease],
    credential: CredentialLease,
) -> None:
    release_task = asyncio.create_task(credentials.release(credential))
    try:
        await asyncio.shield(release_task)
    except asyncio.CancelledError:
        await release_task
        raise
