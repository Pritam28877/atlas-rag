import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path

import pytest

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderCompleted,
    ProviderContextPlan,
    ProviderTextDelta,
    ProviderUsage,
)
from app.services.harness.providers.credential_material import CredentialLease
from app.services.harness.providers.egress_contracts import ProviderEgressRequest
from app.services.harness.providers.vertex_compiler import (
    VertexGenerateContentCompiler,
)
from app.services.harness.providers.vertex_decoder import (
    VertexGenerateContentDecoder,
)
from app.services.harness.providers.vertex_policy import AuthorizedVertexRoute
from app.services.harness.providers.vertex_stream_transport import (
    BoundedVertexGenerateContentTransport,
    VertexTransportError,
    VertexTransportErrorCode,
)
from tests.harness.providers.vertex.fixtures import (
    DESTINATION_SHA256,
    HANDLE,
    authorized_route,
    configuration,
)
from tests.harness_provider_capability_fixtures import NOW, request

FIXTURES = (
    Path(__file__).parents[3]
    / "fixtures"
    / "harness"
    / "vertex_generate_content"
)


class RecordingConnector:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.requests: list[ProviderEgressRequest] = []
        self.closed = False

    def stream(
        self,
        egress_request: ProviderEgressRequest,
        route: AuthorizedVertexRoute,
        credential: CredentialLease,
        *,
        cancellation: asyncio.Event,
        deadline_at,
    ) -> AsyncGenerator[bytes, None]:
        del route, credential, cancellation, deadline_at
        self.requests.append(egress_request)
        return self._stream()

    async def _stream(self) -> AsyncGenerator[bytes, None]:
        try:
            for chunk in self.chunks:
                yield chunk
        finally:
            self.closed = True


def test_vertex_stream_normalizes_sse_and_binds_regional_request() -> None:
    connector = RecordingConnector(_happy_sse_chunks())
    transport = BoundedVertexGenerateContentTransport(
        connector,
        clock=lambda: NOW,
    )

    events = asyncio.run(_collect(transport))

    assert any(isinstance(event, ProviderTextDelta) for event in events)
    assert any(isinstance(event, ProviderUsage) for event in events)
    assert isinstance(events[-1], ProviderCompleted)
    assert connector.closed
    egress = connector.requests[0]
    route = authorized_route()
    assert egress.metadata.target_url == f"{route.stream_url}?alt=sse"
    assert egress.metadata.provider == "vertex"
    assert tuple(
        (header.name, header.value)
        for header in egress.metadata.safe_headers
    ) == (
        ("accept", "text/event-stream"),
        ("x-goog-user-project", route.project_id),
    )
    body = json.loads(egress.body())
    assert body["contents"][0]["role"] == "user"
    assert body["generationConfig"]["maxOutputTokens"] == 1_024


def test_vertex_stream_cancellation_is_terminal_and_closes_body() -> None:
    connector = RecordingConnector(_happy_sse_chunks())
    transport = BoundedVertexGenerateContentTransport(
        connector,
        clock=lambda: NOW,
    )

    events = asyncio.run(_collect(transport, cancel_after_first=True))

    assert isinstance(events[-1], ProviderCancelled)
    assert connector.closed


def test_vertex_stream_rejects_model_truncation_and_pre_cancel() -> None:
    connector = RecordingConnector(_happy_sse_chunks())
    transport = BoundedVertexGenerateContentTransport(
        connector,
        clock=lambda: NOW,
    )
    wrong_model = _compiled().model_copy(update={"model": "other-model"})
    with pytest.raises(VertexTransportError) as model_error:
        asyncio.run(_collect(transport, compiled=wrong_model))
    assert model_error.value.code is VertexTransportErrorCode.MODEL

    truncated = RecordingConnector((b"data: {\"partial\":true}",))
    truncated_transport = BoundedVertexGenerateContentTransport(
        truncated,
        clock=lambda: NOW,
    )
    with pytest.raises(VertexTransportError) as stream_error:
        asyncio.run(_collect(truncated_transport))
    assert stream_error.value.code is VertexTransportErrorCode.PROVIDER
    assert truncated.closed

    cancelled = asyncio.Event()
    cancelled.set()
    with pytest.raises(VertexTransportError) as cancellation_error:
        asyncio.run(
            _collect(
                transport,
                cancellation=cancelled,
            )
        )
    assert (
        cancellation_error.value.code
        is VertexTransportErrorCode.CANCELLED
    )


async def _collect(
    transport: BoundedVertexGenerateContentTransport,
    *,
    cancel_after_first: bool = False,
    cancellation: asyncio.Event | None = None,
    compiled=None,
):
    selected_cancellation = cancellation or asyncio.Event()
    events = []
    async for event in transport.stream(
        _canonical_request(),
        compiled or _compiled(),
        authorized_route(),
        _credential(),
        VertexGenerateContentDecoder(_zero_cost),
        cancellation=selected_cancellation,
        deadline_at=NOW + timedelta(seconds=30),
    ):
        events.append(event)
        if cancel_after_first and len(events) == 1:
            selected_cancellation.set()
    return events


def _canonical_request():
    return request().model_copy(
        update={
            "route_id": "vertex.primary",
            "deadline_at": NOW + timedelta(seconds=30),
        }
    )


def _compiled():
    canonical_request = _canonical_request()
    provider_model = configuration()[0].configuration.models[0]
    context = ProviderContextPlan(
        provider_request_sha256=hashlib.sha256(
            canonical_request.model_dump_json().encode()
        ).hexdigest(),
        model_revision_sha256=provider_model.model_revision_sha256,
        applied_features=(),
        estimated_input_tokens=10,
        reason="Vertex transport fixture context.",
    )
    return VertexGenerateContentCompiler().compile(
        canonical_request,
        provider_model,
        context,
    )


def _credential() -> CredentialLease:
    return CredentialLease(
        broker_identity=object(),
        lease_id=1,
        handle=HANDLE,
        provider="vertex",
        destination_sha256=DESTINATION_SHA256,
        expires_at=NOW + timedelta(minutes=1),
        secret=bytearray(b"vertex-access-token"),
    )


def _happy_sse_chunks() -> tuple[bytes, ...]:
    records = (FIXTURES / "happy.jsonl").read_bytes().splitlines()
    return tuple(b"data: " + record + b"\n\n" for record in records)


def _zero_cost(
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
) -> int:
    del input_tokens, cached_tokens, output_tokens, reasoning_tokens
    return 0
