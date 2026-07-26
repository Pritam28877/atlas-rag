import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.services.harness.protocol import (
    DataClassification,
    ProviderFailureClass,
    ProviderRetryDisposition,
    ProviderTransportError,
)
from app.services.harness.providers import (
    GatewayProviderAttemptExecutor,
    ProviderEgressGatewayError,
    ProviderEgressGatewayErrorCode,
    ProviderEgressPolicy,
    ProviderEgressRequest,
    ProviderEgressResponse,
    SafeEgressHeader,
    provider_destination_sha256,
    provider_egress_request_sha256,
)

NOW = datetime(2026, 7, 27, 19, 0, tzinfo=UTC)
DESTINATION_URL = "https://api.provider.example/v1"


class Gateway:
    def __init__(
        self,
        outcome: ProviderEgressResponse | ProviderEgressGatewayError,
    ) -> None:
        self.outcome = outcome
        self.attempts: list[int] = []

    async def send(
        self,
        request,
        policy,
        *,
        cancellation,
        deadline_at,
        attempt,
    ) -> ProviderEgressResponse:
        self.attempts.append(attempt)
        if isinstance(self.outcome, ProviderEgressGatewayError):
            raise self.outcome
        return self.outcome


def request() -> ProviderEgressRequest:
    return ProviderEgressRequest(
        request_id="req_" + "1" * 32,
        provider="configured-provider",
        credential_handle="pcr_" + "2" * 32,
        target_url=f"{DESTINATION_URL}/responses",
        classification=DataClassification.CONFIDENTIAL,
        content_type="application/json",
        safe_headers=(),
        body=b"{}",
    )


def policy() -> ProviderEgressPolicy:
    return ProviderEgressPolicy(
        provider="configured-provider",
        destination_url=DESTINATION_URL,
        destination_sha256=provider_destination_sha256(DESTINATION_URL),
        allowed_redirect_origins=(),
        accepted_classifications=(DataClassification.CONFIDENTIAL,),
        max_request_bytes=1_024,
        max_response_bytes=1_024,
    )


def executor(
    outcome: ProviderEgressResponse | ProviderEgressGatewayError,
) -> GatewayProviderAttemptExecutor:
    return GatewayProviderAttemptExecutor(
        Gateway(outcome),
        request(),
        policy(),
        lambda response: 30,
        clock=lambda: NOW,
    )


def test_success_returns_bounded_response_and_actual_cost() -> None:
    async def scenario() -> None:
        success = await executor(
            ProviderEgressResponse(status=200, headers=(), body=b"ok")
        ).execute(
            1,
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=1),
        )
        assert success.value.body() == b"ok"
        assert success.actual_cost_microusd == 30

    asyncio.run(scenario())


def test_rate_limit_preserves_bounded_retry_hint() -> None:
    async def scenario() -> None:
        with pytest.raises(ProviderTransportError) as captured:
            await executor(
                ProviderEgressResponse(
                    status=429,
                    headers=(SafeEgressHeader(name="retry-after", value="2"),),
                    body=b"",
                )
            ).execute(
                1,
                cancellation=asyncio.Event(),
                deadline_at=NOW + timedelta(seconds=1),
            )
        failure = captured.value.failure
        assert failure.failure_class is ProviderFailureClass.RATE_LIMIT
        assert failure.retry_disposition is ProviderRetryDisposition.ELIGIBLE
        assert failure.retry_after_ms == 2_000
        assert not failure.ambiguous
        assert failure.provider_request_sha256 == (
            provider_egress_request_sha256(request())
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("code", "eligible", "ambiguous"),
    [
        (ProviderEgressGatewayErrorCode.RESOLUTION, True, False),
        (ProviderEgressGatewayErrorCode.CONNECTOR, False, True),
    ],
)
def test_gateway_failure_classification_is_conservative(
    code: ProviderEgressGatewayErrorCode,
    eligible: bool,
    ambiguous: bool,
) -> None:
    async def scenario() -> None:
        with pytest.raises(ProviderTransportError) as captured:
            await executor(ProviderEgressGatewayError(code)).execute(
                1,
                cancellation=asyncio.Event(),
                deadline_at=NOW + timedelta(seconds=1),
            )
        failure = captured.value.failure
        assert (
            failure.retry_disposition is ProviderRetryDisposition.ELIGIBLE
        ) is eligible
        assert failure.ambiguous is ambiguous

    asyncio.run(scenario())
