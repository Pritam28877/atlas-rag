"""Typed attempt adapter around the audited provider egress gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from app.services.harness.protocol import (
    ProviderFailureClass,
    ProviderRetryDisposition,
    ProviderTransportError,
    ProviderTransportFailure,
)
from app.services.harness.providers.dispatch_contracts import (
    ProviderAttemptSuccess,
)
from app.services.harness.providers.egress_contracts import (
    ProviderEgressRequest,
    ProviderEgressResponse,
    provider_egress_request_sha256,
)
from app.services.harness.providers.egress_gateway import (
    ProviderEgressGatewayError,
    ProviderEgressGatewayErrorCode,
)
from app.services.harness.providers.egress_policy import ProviderEgressPolicy

RETRYABLE_PROVIDER_STATUSES = frozenset({429, 500, 502, 503, 504})


class ProviderGateway(Protocol):
    async def send(
        self,
        request: ProviderEgressRequest,
        policy: ProviderEgressPolicy,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
        attempt: int,
    ) -> ProviderEgressResponse: ...


class GatewayProviderAttemptExecutor:
    def __init__(
        self,
        gateway: ProviderGateway,
        request: ProviderEgressRequest,
        policy: ProviderEgressPolicy,
        actual_cost: Callable[[ProviderEgressResponse], int],
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._gateway = gateway
        self._request = request
        self._policy = policy
        self._actual_cost = actual_cost
        self._clock = clock

    async def execute(
        self,
        attempt: int,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> ProviderAttemptSuccess[ProviderEgressResponse]:
        request_sha256 = provider_egress_request_sha256(self._request)
        try:
            response = await self._gateway.send(
                self._request,
                self._policy,
                cancellation=cancellation,
                deadline_at=deadline_at,
                attempt=attempt,
            )
        except ProviderEgressGatewayError as error:
            raise ProviderTransportError(
                _gateway_failure(
                    error.code,
                    request_sha256,
                    self._clock(),
                )
            ) from None
        except Exception:
            raise ProviderTransportError(
                _ambiguous_failure(
                    request_sha256,
                    self._clock(),
                )
            ) from None
        if not 200 <= response.status <= 299:
            raise ProviderTransportError(
                _response_failure(
                    response,
                    request_sha256,
                    self._clock(),
                )
            )
        return ProviderAttemptSuccess(
            value=response,
            actual_cost_microusd=self._actual_cost(response),
        )


def _gateway_failure(
    code: ProviderEgressGatewayErrorCode,
    request_sha256: str,
    occurred_at: datetime,
) -> ProviderTransportFailure:
    if code is ProviderEgressGatewayErrorCode.RESOLUTION:
        return ProviderTransportFailure(
            failure_class=ProviderFailureClass.TRANSIENT,
            retry_disposition=ProviderRetryDisposition.ELIGIBLE,
            code="provider_dns_resolution",
            reason="Provider address resolution failed before dispatch.",
            provider_request_sha256=request_sha256,
            ambiguous=False,
            occurred_at=occurred_at,
        )
    ambiguous = code in {
        ProviderEgressGatewayErrorCode.AUDIT,
        ProviderEgressGatewayErrorCode.CONNECTOR,
    }
    return ProviderTransportFailure(
        failure_class=ProviderFailureClass.INTERNAL,
        retry_disposition=ProviderRetryDisposition.PROHIBITED,
        code=f"provider_gateway_{code.value}",
        reason="Provider gateway attempt failed.",
        provider_request_sha256=request_sha256,
        ambiguous=ambiguous,
        occurred_at=occurred_at,
    )


def _ambiguous_failure(
    request_sha256: str,
    occurred_at: datetime,
) -> ProviderTransportFailure:
    return ProviderTransportFailure(
        failure_class=ProviderFailureClass.INTERNAL,
        retry_disposition=ProviderRetryDisposition.PROHIBITED,
        code="provider_gateway_unknown",
        reason="Provider gateway attempt state is ambiguous.",
        provider_request_sha256=request_sha256,
        ambiguous=True,
        occurred_at=occurred_at,
    )


def _response_failure(
    response: ProviderEgressResponse,
    request_sha256: str,
    occurred_at: datetime,
) -> ProviderTransportFailure:
    retryable = response.status in RETRYABLE_PROVIDER_STATUSES
    rate_limited = response.status == 429
    return ProviderTransportFailure(
        failure_class=(
            ProviderFailureClass.RATE_LIMIT
            if rate_limited
            else (
                ProviderFailureClass.TRANSIENT
                if retryable
                else ProviderFailureClass.MALFORMED
            )
        ),
        retry_disposition=(
            ProviderRetryDisposition.ELIGIBLE
            if retryable
            else ProviderRetryDisposition.PROHIBITED
        ),
        code=f"provider_http_{response.status}",
        reason="Provider returned a non-success response.",
        provider_request_sha256=request_sha256,
        http_status=response.status,
        retry_after_ms=_retry_after_ms(response) if retryable else None,
        ambiguous=False,
        occurred_at=occurred_at,
    )


def _retry_after_ms(response: ProviderEgressResponse) -> int | None:
    for header in response.headers:
        if header.name != "retry-after":
            continue
        try:
            seconds = int(header.value)
        except ValueError:
            return None
        if not 0 <= seconds <= 3_600:
            return None
        return seconds * 1_000
    return None
