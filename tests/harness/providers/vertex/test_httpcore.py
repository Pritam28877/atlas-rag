import asyncio
from datetime import timedelta

import pytest

from app.services.harness.protocol import DataClassification
from app.services.harness.providers.credential_material import CredentialLease
from app.services.harness.providers.egress_contracts import (
    ProviderEgressRequest,
    SafeEgressHeader,
)
from app.services.harness.providers.provider_sse_httpcore_support import (
    SseHttpCoreError,
    SseHttpCoreErrorCode,
)
from app.services.harness.providers.vertex_httpcore import (
    VertexHttpCoreConnector,
)
from tests.harness.providers.common.httpcore_fixtures import (
    RecordingBackend,
    RecordingCredentialEncoder,
    Resolver,
    sse_response,
)
from tests.harness.providers.vertex.fixtures import (
    DESTINATION_SHA256,
    HANDLE,
    PROJECT_ID,
    authorized_route,
)
from tests.harness_provider_capability_fixtures import NOW


def test_vertex_connector_pins_region_and_zeros_bearer_header() -> None:
    backend = RecordingBackend([sse_response()])
    resolver = Resolver()
    encoder = RecordingCredentialEncoder()
    connector = VertexHttpCoreConnector(
        resolver,
        credential_encoder=encoder,
        backend_factory=lambda target: backend,
        clock=lambda: NOW,
    )

    chunks = asyncio.run(_collect(connector))

    assert b"".join(chunks) == b"data: {}\n\n"
    assert resolver.calls == [
        ("us-central1-aiplatform.googleapis.com", 443)
    ]
    assert backend.connections == [
        ("us-central1-aiplatform.googleapis.com", 443)
    ]
    written = b"".join(backend.stream.writes)
    assert b":streamGenerateContent?alt=sse HTTP/1.1" in written
    assert b"authorization: Bearer vertex-access-token" in written
    assert b"x-goog-user-project: atlas-test-12345" in written
    assert encoder.buffers[0] == bytearray(len(encoder.buffers[0]))


def test_vertex_connector_rejects_private_dns_redirect_and_expiry() -> None:
    private_connector = VertexHttpCoreConnector(
        Resolver(("10.0.0.5",)),
        backend_factory=lambda target: RecordingBackend([sse_response()]),
        clock=lambda: NOW,
    )
    with pytest.raises(SseHttpCoreError) as private:
        asyncio.run(_collect(private_connector))
    assert private.value.code is SseHttpCoreErrorCode.RESOLUTION

    redirect_connector = VertexHttpCoreConnector(
        Resolver(),
        backend_factory=lambda target: RecordingBackend(
            [sse_response(status=b"307 Temporary Redirect")]
        ),
        clock=lambda: NOW,
    )
    with pytest.raises(SseHttpCoreError) as redirect:
        asyncio.run(_collect(redirect_connector))
    assert redirect.value.code is SseHttpCoreErrorCode.REDIRECT

    expired_connector = VertexHttpCoreConnector(
        Resolver(),
        backend_factory=lambda target: RecordingBackend([sse_response()]),
        clock=lambda: NOW,
    )
    with pytest.raises(SseHttpCoreError) as expired:
        asyncio.run(
            _collect(
                expired_connector,
                credential=_credential(
                    expires_at=NOW - timedelta(seconds=1)
                ),
            )
        )
    assert expired.value.code is SseHttpCoreErrorCode.AUTHENTICATION


def test_vertex_connector_rejects_request_substitution_and_pre_cancel() -> None:
    connector = VertexHttpCoreConnector(
        Resolver(),
        backend_factory=lambda target: RecordingBackend([sse_response()]),
        clock=lambda: NOW,
    )
    substituted = _request().metadata.model_copy(
        update={"target_url": "https://attacker.example/stream"}
    )
    original = _request()
    request = ProviderEgressRequest(
        request_id=substituted.request_id,
        provider=substituted.provider,
        credential_handle=substituted.credential_handle,
        target_url=substituted.target_url,
        classification=substituted.classification,
        content_type=substituted.content_type,
        safe_headers=substituted.safe_headers,
        body=original.body(),
    )
    with pytest.raises(SseHttpCoreError) as destination:
        asyncio.run(_collect(connector, request=request))
    assert destination.value.code is SseHttpCoreErrorCode.DESTINATION

    cancellation = asyncio.Event()
    cancellation.set()
    with pytest.raises(SseHttpCoreError) as cancelled:
        asyncio.run(_collect(connector, cancellation=cancellation))
    assert cancelled.value.code is SseHttpCoreErrorCode.CANCELLED


async def _collect(
    connector: VertexHttpCoreConnector,
    *,
    request: ProviderEgressRequest | None = None,
    credential: CredentialLease | None = None,
    cancellation: asyncio.Event | None = None,
) -> tuple[bytes, ...]:
    chunks = []
    async for chunk in connector.stream(
        request or _request(),
        authorized_route(),
        credential or _credential(),
        cancellation=cancellation or asyncio.Event(),
        deadline_at=NOW + timedelta(seconds=30),
    ):
        chunks.append(chunk)
    return tuple(chunks)


def _request() -> ProviderEgressRequest:
    route = authorized_route()
    return ProviderEgressRequest(
        request_id="req_" + "1" * 32,
        provider="vertex",
        credential_handle=HANDLE,
        target_url=f"{route.stream_url}?alt=sse",
        classification=DataClassification.PUBLIC,
        content_type="application/json",
        safe_headers=(
            SafeEgressHeader(name="accept", value="text/event-stream"),
            SafeEgressHeader(
                name="x-goog-user-project",
                value=PROJECT_ID,
            ),
        ),
        body=b"{}",
    )


def _credential(
    *,
    expires_at=None,
) -> CredentialLease:
    return CredentialLease(
        broker_identity=object(),
        lease_id=1,
        handle=HANDLE,
        provider="vertex",
        destination_sha256=DESTINATION_SHA256,
        expires_at=expires_at or NOW + timedelta(minutes=1),
        secret=bytearray(b"vertex-access-token"),
    )
