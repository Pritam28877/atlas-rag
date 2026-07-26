import asyncio
from datetime import UTC, datetime, timedelta

import httpcore
import pytest

from app.services.harness.protocol import DataClassification
from app.services.harness.providers import (
    HttpCoreConnectorError,
    HttpCoreConnectorErrorCode,
    HttpCoreEgressConnector,
    ProviderEgressPolicy,
    ProviderEgressRequest,
    SafeEgressHeader,
    authorize_egress_target,
    provider_destination_sha256,
)
from app.services.harness.providers.credential_material import CredentialLease

DESTINATION_URL = "https://api.provider.example/v1"


class RecordingStream(httpcore.AsyncMockStream):
    def __init__(self, response_parts: list[bytes]) -> None:
        super().__init__(response_parts)
        self.writes: list[bytes] = []

    async def write(
        self,
        buffer: bytes,
        timeout: float | None = None,
    ) -> None:
        self.writes.append(buffer)


class RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, response_parts: list[bytes]) -> None:
        self.stream = RecordingStream(response_parts)

    async def connect_tcp(
        self,
        host,
        port,
        timeout=None,
        local_address=None,
        socket_options=None,
    ):
        return self.stream

    async def connect_unix_socket(
        self,
        path,
        timeout=None,
        socket_options=None,
    ):
        raise AssertionError("unexpected Unix socket")

    async def sleep(self, seconds: float) -> None:
        return None


class Encoder:
    def __init__(self) -> None:
        self.buffers: list[bytearray] = []

    def encode(
        self,
        credential: CredentialLease,
    ) -> tuple[bytes, bytearray]:
        value = bytearray(b"Bearer ")
        value.extend(credential.secret_view())
        self.buffers.append(value)
        return b"authorization", value


def target():
    destination_sha256 = provider_destination_sha256(DESTINATION_URL)
    configured_policy = ProviderEgressPolicy(
        provider="configured-provider",
        destination_url=DESTINATION_URL,
        destination_sha256=destination_sha256,
        allowed_redirect_origins=(),
        accepted_classifications=(DataClassification.CONFIDENTIAL,),
        max_request_bytes=1_024,
        max_response_bytes=1_024,
    )
    return authorize_egress_target(
        configured_policy,
        target_url="https://api.provider.example/v1/responses",
        classification=DataClassification.CONFIDENTIAL,
        request_bytes=2,
        resolved_addresses=("1.1.1.1",),
        redirect_count=0,
    )


def request() -> ProviderEgressRequest:
    return ProviderEgressRequest(
        request_id="req_" + "1" * 32,
        provider="configured-provider",
        credential_handle="pcr_" + "9" * 32,
        target_url="https://api.provider.example/v1/responses",
        classification=DataClassification.CONFIDENTIAL,
        content_type="application/json",
        safe_headers=(SafeEgressHeader(name="accept", value="application/json"),),
        body=b"{}",
    )


def credential() -> CredentialLease:
    return CredentialLease(
        broker_identity=object(),
        lease_id=1,
        handle="pcr_" + "9" * 32,
        provider="configured-provider",
        destination_sha256=provider_destination_sha256(DESTINATION_URL),
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        secret=bytearray(b"canary-token"),
    )


def test_connector_streams_bounded_response_filters_headers_and_zeros_auth() -> None:
    async def scenario() -> None:
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 2\r\n"
            b"X-Request-ID: safe-id\r\n"
            b"Set-Cookie: secret-cookie\r\n\r\n"
            b"ok"
        )
        backend = RecordingBackend([response])
        encoder = Encoder()
        connector = HttpCoreEgressConnector(
            encoder,
            backend_factory=lambda authorized: backend,
        )

        result = await connector.send(
            request(),
            target(),
            credential(),
            cancellation=asyncio.Event(),
            deadline_at=_deadline(),
            max_response_bytes=10,
        )

        written = b"".join(backend.stream.writes)
        assert result.status == 200
        assert result.body() == b"ok"
        assert tuple(header.name for header in result.headers) == (
            "x-request-id",
        )
        assert b"Bearer canary-token" in written
        assert encoder.buffers[0] == bytearray(len(encoder.buffers[0]))

    asyncio.run(scenario())


def test_connector_omits_auth_on_credentialless_redirect_hop() -> None:
    async def scenario() -> None:
        response = (
            b"HTTP/1.1 307 Temporary Redirect\r\n"
            b"Content-Length: 0\r\n"
            b"Location: https://uploads.provider.example/upload\r\n\r\n"
        )
        backend = RecordingBackend([response])
        encoder = Encoder()
        connector = HttpCoreEgressConnector(
            encoder,
            backend_factory=lambda authorized: backend,
        )

        result = await connector.send(
            request(),
            target(),
            None,
            cancellation=asyncio.Event(),
            deadline_at=_deadline(),
            max_response_bytes=10,
        )

        assert result.redirect_url == (
            "https://uploads.provider.example/upload"
        )
        assert b"authorization:" not in b"".join(backend.stream.writes).lower()
        assert encoder.buffers == []

    asyncio.run(scenario())


def test_connector_enforces_response_limit_and_pre_cancelled_request() -> None:
    async def scenario() -> None:
        backend = RecordingBackend(
            [b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc"]
        )
        connector = HttpCoreEgressConnector(
            Encoder(),
            backend_factory=lambda authorized: backend,
        )
        with pytest.raises(HttpCoreConnectorError) as oversized:
            await connector.send(
                request(),
                target(),
                None,
                cancellation=asyncio.Event(),
                deadline_at=_deadline(),
                max_response_bytes=2,
            )
        cancellation = asyncio.Event()
        cancellation.set()
        with pytest.raises(HttpCoreConnectorError) as cancelled:
            await connector.send(
                request(),
                target(),
                None,
                cancellation=cancellation,
                deadline_at=_deadline(),
                max_response_bytes=10,
            )

        assert oversized.value.code is HttpCoreConnectorErrorCode.RESPONSE_SIZE
        assert cancelled.value.code is HttpCoreConnectorErrorCode.CANCELLED

    asyncio.run(scenario())


def _deadline() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=5)
