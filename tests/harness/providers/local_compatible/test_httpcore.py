import asyncio
from datetime import timedelta

import pytest

from app.services.harness.providers.credential_material import CredentialLease
from app.services.harness.providers.egress_contracts import (
    ProviderEgressRequest,
    SafeEgressHeader,
)
from app.services.harness.providers.local_compatible_httpcore import (
    LocalCompatibleHttpCoreConnector,
)
from app.services.harness.providers.local_compatible_httpcore_support import (
    LocalHttpCoreError,
    LocalHttpCoreErrorCode,
)
from app.services.harness.providers.local_compatible_identity import (
    LocalAuthenticationMode,
)
from app.services.harness.providers.local_compatible_policy import (
    LocalEndpointMode,
    local_destination_sha256,
)
from tests.harness.providers.common.httpcore_fixtures import (
    RecordingBackend,
    RecordingCredentialEncoder,
    Resolver,
    sse_response,
)
from tests.harness.providers.local_compatible.fixtures import (
    NOW,
    authorized_route,
    local_request,
)


def test_loopback_is_pinned_without_dns_and_streams_sse() -> None:
    backend = RecordingBackend([sse_response(body=b"data: [DONE]\n\n")])
    resolver = Resolver()
    connector = LocalCompatibleHttpCoreConnector(
        resolver,
        backend_factory=lambda target: backend,
        clock=lambda: NOW,
    )

    chunks = asyncio.run(_collect(connector, _request(), authorized_route()))

    assert b"".join(chunks) == b"data: [DONE]\n\n"
    assert resolver.calls == []
    assert backend.connections == [("127.0.0.1", 11434)]
    written = b"".join(backend.stream.writes)
    assert b"POST /v1/responses HTTP/1.1" in written
    assert b"authorization:" not in written.lower()


def test_content_type_redirect_and_pre_cancellation_fail_closed() -> None:
    for response, code in (
        (
            sse_response(content_type=b"application/json"),
            LocalHttpCoreErrorCode.CONTENT_TYPE,
        ),
        (
            sse_response(status=b"307 Temporary Redirect"),
            LocalHttpCoreErrorCode.REDIRECT,
        ),
    ):
        connector = LocalCompatibleHttpCoreConnector(
            Resolver(),
            backend_factory=lambda target, raw=response: RecordingBackend([raw]),
            clock=lambda: NOW,
        )
        with pytest.raises(LocalHttpCoreError) as captured:
            asyncio.run(_collect(connector, _request(), authorized_route()))
        assert captured.value.code is code

    cancellation = asyncio.Event()
    cancellation.set()
    connector = LocalCompatibleHttpCoreConnector(
        Resolver(),
        backend_factory=lambda target: RecordingBackend([sse_response()]),
        clock=lambda: NOW,
    )
    with pytest.raises(LocalHttpCoreError) as cancelled:
        asyncio.run(
            _collect(
                connector,
                _request(),
                authorized_route(),
                cancellation=cancellation,
            )
        )
    assert cancelled.value.code is LocalHttpCoreErrorCode.CANCELLED


def test_bearer_mode_requires_bound_credential_and_zeros_header() -> None:
    backend = RecordingBackend([sse_response()])
    encoder = RecordingCredentialEncoder()
    route = authorized_route().model_copy(
        update={
            "authentication": LocalAuthenticationMode.BEARER_ENVIRONMENT
        }
    )
    connector = LocalCompatibleHttpCoreConnector(
        Resolver(),
        credential_encoder=encoder,
        backend_factory=lambda target: backend,
        clock=lambda: NOW,
    )

    asyncio.run(
        _collect(
            connector,
            _request(),
            route,
            credential=_credential(),
        )
    )

    assert b"Bearer local-secret" in b"".join(backend.stream.writes)
    assert encoder.buffers[0] == bytearray(len(encoder.buffers[0]))
    with pytest.raises(LocalHttpCoreError) as missing:
        asyncio.run(_collect(connector, _request(), route))
    assert missing.value.code is LocalHttpCoreErrorCode.AUTHENTICATION


def test_remote_tls_rejects_private_dns_resolution() -> None:
    endpoint = "https://models.example/v1/"
    route = authorized_route().model_copy(
        update={
            "endpoint_mode": LocalEndpointMode.REMOTE_TLS,
            "endpoint_url": endpoint,
            "responses_url": f"{endpoint}responses",
            "destination_sha256": local_destination_sha256(endpoint),
        }
    )
    connector = LocalCompatibleHttpCoreConnector(
        Resolver(("10.0.0.5",)),
        backend_factory=lambda target: RecordingBackend([sse_response()]),
        clock=lambda: NOW,
    )

    with pytest.raises(LocalHttpCoreError) as captured:
        asyncio.run(_collect(connector, _request(route), route))

    assert captured.value.code is LocalHttpCoreErrorCode.RESOLUTION


async def _collect(
    connector: LocalCompatibleHttpCoreConnector,
    request: ProviderEgressRequest,
    route,
    *,
    credential: CredentialLease | None = None,
    cancellation: asyncio.Event | None = None,
) -> tuple[bytes, ...]:
    chunks = []
    async for chunk in connector.stream(
        request,
        route,
        credential,
        cancellation=cancellation or asyncio.Event(),
        deadline_at=NOW + timedelta(seconds=30),
    ):
        chunks.append(chunk)
    return tuple(chunks)


def _request(route=None) -> ProviderEgressRequest:
    selected_route = route or authorized_route()
    canonical = local_request()
    return ProviderEgressRequest(
        request_id=canonical.request_id,
        provider="local-compatible",
        credential_handle=selected_route.credential_handle,
        target_url=selected_route.responses_url,
        classification=canonical.classification,
        content_type="application/json",
        safe_headers=(
            SafeEgressHeader(name="accept", value="text/event-stream"),
        ),
        body=b"{}",
    )


def _credential() -> CredentialLease:
    route = authorized_route()
    return CredentialLease(
        broker_identity=object(),
        lease_id=1,
        handle=route.credential_handle,
        provider="local-compatible",
        destination_sha256=route.destination_sha256,
        expires_at=NOW + timedelta(minutes=1),
        secret=bytearray(b"local-secret"),
    )
