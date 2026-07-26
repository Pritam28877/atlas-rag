import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.services.harness.protocol import DataClassification
from app.services.harness.providers import (
    EgressAuditOutcome,
    PayloadInspection,
    ProviderEgressGateway,
    ProviderEgressGatewayError,
    ProviderEgressGatewayErrorCode,
    ProviderEgressPolicy,
    ProviderEgressRequest,
    ProviderEgressResponse,
    SafeEgressHeader,
    provider_destination_sha256,
)
from app.services.harness.providers.credential_material import CredentialLease

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
DESTINATION_URL = "https://api.provider.example/v1"
DIGEST = provider_destination_sha256(DESTINATION_URL)
CANARY = b"prompt-canary-must-not-enter-audit"


class Credentials:
    def __init__(self) -> None:
        self.acquisitions: list[tuple[str, str, str]] = []
        self.released: list[CredentialLease] = []

    async def acquire(
        self,
        handle: str,
        *,
        provider: str,
        destination_sha256: str,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> CredentialLease:
        self.acquisitions.append((handle, provider, destination_sha256))
        return CredentialLease(
            broker_identity=self,
            lease_id=1,
            handle=handle,
            provider=provider,
            destination_sha256=destination_sha256,
            expires_at=deadline_at,
            secret=bytearray(b"temporary-secret"),
        )

    async def refresh(
        self,
        credential: CredentialLease,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> CredentialLease:
        raise AssertionError("gateway does not refresh credentials")

    async def release(self, credential: CredentialLease) -> None:
        credential.release()
        self.released.append(credential)


class Resolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def resolve(
        self,
        hostname: str,
        port: int,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> tuple[str, ...]:
        self.calls.append((hostname, port))
        return ("1.1.1.1",)


class Inspector:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls = 0

    async def inspect(
        self,
        request: ProviderEgressRequest,
    ) -> PayloadInspection:
        self.calls += 1
        return PayloadInspection(
            allowed=self.allowed,
            policy_revision_sha256="1" * 64,
            reason="Injected payload inspection result.",
        )


class Connector:
    def __init__(self, responses: list[ProviderEgressResponse]) -> None:
        self.responses = responses
        self.credential_presence: list[bool] = []
        self.targets: list[str] = []

    async def send(
        self,
        request,
        target,
        credential,
        *,
        cancellation,
        deadline_at,
        max_response_bytes,
    ) -> ProviderEgressResponse:
        self.targets.append(target.canonical_url)
        self.credential_presence.append(credential is not None)
        return self.responses.pop(0)


class Audit:
    def __init__(self, *, fail: bool = False) -> None:
        self.records = []
        self.fail = fail

    async def record(self, record) -> None:
        if self.fail:
            raise RuntimeError("audit detail")
        self.records.append(record)


def request(
    *,
    target_url: str = "https://api.provider.example/v1/responses",
    provider: str = "configured-provider",
) -> ProviderEgressRequest:
    return ProviderEgressRequest(
        request_id="req_" + "1" * 32,
        provider=provider,
        credential_handle="pcr_" + "9" * 32,
        target_url=target_url,
        classification=DataClassification.CONFIDENTIAL,
        content_type="application/json",
        safe_headers=(SafeEgressHeader(name="accept", value="application/json"),),
        body=CANARY,
    )


def policy() -> ProviderEgressPolicy:
    return ProviderEgressPolicy(
        provider="configured-provider",
        destination_url=DESTINATION_URL,
        destination_sha256=DIGEST,
        allowed_redirect_origins=("https://uploads.provider.example",),
        accepted_classifications=(DataClassification.CONFIDENTIAL,),
        max_request_bytes=1_024,
        max_response_bytes=1_024,
        max_redirects=2,
    )


def gateway(
    *,
    credentials: Credentials,
    resolver: Resolver,
    inspector: Inspector,
    connector: Connector,
    audit: Audit,
) -> ProviderEgressGateway:
    return ProviderEgressGateway(
        credentials=credentials,
        resolver=resolver,
        inspector=inspector,
        connector=connector,
        audit=audit,
        clock=lambda: NOW,
    )


def test_gateway_orders_checks_audits_and_releases_secret() -> None:
    async def scenario() -> None:
        credentials = Credentials()
        resolver = Resolver()
        inspector = Inspector()
        connector = Connector(
            [ProviderEgressResponse(status=200, headers=(), body=b"ok")]
        )
        audit = Audit()

        response = await gateway(
            credentials=credentials,
            resolver=resolver,
            inspector=inspector,
            connector=connector,
            audit=audit,
        ).send(
            request(),
            policy(),
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=1),
            attempt=1,
        )

        assert response.body() == b"ok"
        assert inspector.calls == 1
        assert resolver.calls == [("api.provider.example", 443)]
        assert credentials.acquisitions == [
            ("pcr_" + "9" * 32, "configured-provider", DIGEST)
        ]
        assert credentials.released[0].released
        assert connector.credential_presence == [True]
        assert [record.outcome for record in audit.records] == [
            EgressAuditOutcome.AUTHORIZED,
            EgressAuditOutcome.SUCCEEDED,
        ]
        audit_json = " ".join(
            record.model_dump_json() for record in audit.records
        )
        assert CANARY.decode() not in audit_json
        assert CANARY.decode() not in repr(request())

    asyncio.run(scenario())


def test_cross_origin_redirect_is_reauthorized_without_credential() -> None:
    async def scenario() -> None:
        credentials = Credentials()
        resolver = Resolver()
        connector = Connector(
            [
                ProviderEgressResponse(
                    status=307,
                    headers=(),
                    body=b"",
                    redirect_url="https://uploads.provider.example/upload",
                ),
                ProviderEgressResponse(status=200, headers=(), body=b"done"),
            ]
        )
        audit = Audit()

        response = await gateway(
            credentials=credentials,
            resolver=resolver,
            inspector=Inspector(),
            connector=connector,
            audit=audit,
        ).send(
            request(),
            policy(),
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=1),
            attempt=1,
        )

        assert response.body() == b"done"
        assert resolver.calls == [
            ("api.provider.example", 443),
            ("uploads.provider.example", 443),
        ]
        assert connector.credential_presence == [True, False]
        assert [record.redirect_count for record in audit.records] == [
            0,
            0,
            1,
            1,
        ]
        assert len(credentials.released) == 1

    asyncio.run(scenario())


def test_inspection_and_ssrf_denials_happen_before_resolution() -> None:
    async def scenario() -> None:
        for outgoing_request, inspector in (
            (request(), Inspector(allowed=False)),
            (
                request(target_url="https://127.0.0.1/private"),
                Inspector(),
            ),
        ):
            credentials = Credentials()
            resolver = Resolver()
            with pytest.raises(Exception):
                await gateway(
                    credentials=credentials,
                    resolver=resolver,
                    inspector=inspector,
                    connector=Connector([]),
                    audit=Audit(),
                ).send(
                    outgoing_request,
                    policy(),
                    cancellation=asyncio.Event(),
                    deadline_at=NOW + timedelta(seconds=1),
                    attempt=1,
                )
            assert resolver.calls == []
            assert credentials.acquisitions == []

    asyncio.run(scenario())


def test_response_limit_connector_failure_and_audit_failure_release() -> None:
    async def rejected(
        connector: Connector,
        audit: Audit,
        expected_code: ProviderEgressGatewayErrorCode,
    ) -> None:
        credentials = Credentials()
        with pytest.raises(ProviderEgressGatewayError) as captured:
            await gateway(
                credentials=credentials,
                resolver=Resolver(),
                inspector=Inspector(),
                connector=connector,
                audit=audit,
            ).send(
                request(),
                policy(),
                cancellation=asyncio.Event(),
                deadline_at=NOW + timedelta(seconds=1),
                attempt=1,
            )
        assert captured.value.code is expected_code
        assert len(credentials.released) == 1

    asyncio.run(
        rejected(
            Connector(
                [
                    ProviderEgressResponse(
                        status=200,
                        headers=(),
                        body=b"x" * 1_025,
                    )
                ]
            ),
            Audit(),
            ProviderEgressGatewayErrorCode.RESPONSE_SIZE,
        )
    )
    asyncio.run(
        rejected(
            Connector(
                [ProviderEgressResponse(status=200, headers=(), body=b"ok")]
            ),
            Audit(fail=True),
            ProviderEgressGatewayErrorCode.AUDIT,
        )
    )
