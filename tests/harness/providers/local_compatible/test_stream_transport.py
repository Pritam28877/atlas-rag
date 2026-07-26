import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import timedelta

import pytest

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderCompleted,
    ProviderTextDelta,
)
from app.services.harness.providers.local_compatible_compiler import (
    LocalCompatibleResponsesCompiler,
)
from app.services.harness.providers.local_compatible_sse import (
    MAXIMUM_LOCAL_SSE_LINE_BYTES,
    BoundedSseFramer,
    LocalSseError,
    LocalSseErrorCode,
)
from app.services.harness.providers.local_compatible_stream_transport import (
    BoundedLocalCompatibleResponsesTransport,
    LocalCompatibleTransportError,
    LocalCompatibleTransportErrorCode,
)
from app.services.harness.providers.openai_decoder import (
    OpenAIResponsesDecoder,
)
from tests.harness.providers.local_compatible.fixtures import (
    NOW,
    accepted_decision,
    authorized_route,
    context,
    local_model,
    local_request,
)


class FakeStreamingConnector:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.request_body: bytes | None = None

    async def stream(
        self,
        request,
        route,
        credential,
        *,
        cancellation,
        deadline_at,
    ) -> AsyncGenerator[bytes, None]:
        del route, credential, deadline_at
        self.request_body = request.body()
        for chunk in self.chunks:
            if cancellation.is_set():
                return
            yield chunk


def compiled_request():
    return LocalCompatibleResponsesCompiler().compile(
        local_request(),
        local_model(),
        context(),
        accepted_decision(),
    )


def test_chunked_sse_stream_decodes_and_finishes() -> None:
    connector = FakeStreamingConnector(_completed_sse_chunks())
    transport = BoundedLocalCompatibleResponsesTransport(
        connector,
        clock=lambda: NOW,
    )

    events = asyncio.run(_collect(transport, connector))

    assert isinstance(events[0], ProviderTextDelta)
    assert isinstance(events[-1], ProviderCompleted)
    assert connector.request_body is not None
    body = json.loads(connector.request_body)
    assert body["store"] is False
    assert body["stream"] is True


def test_cancellation_after_first_event_emits_terminal_cancelled() -> None:
    connector = FakeStreamingConnector(_completed_sse_chunks())
    transport = BoundedLocalCompatibleResponsesTransport(
        connector,
        clock=lambda: NOW,
    )

    events = asyncio.run(_collect(transport, connector, cancel_after_first=True))

    assert isinstance(events[0], ProviderTextDelta)
    assert isinstance(events[1], ProviderCancelled)
    assert len(events) == 2


def test_done_before_terminal_and_model_substitution_fail_closed() -> None:
    premature = FakeStreamingConnector((b"data: [DONE]\n\n",))
    transport = BoundedLocalCompatibleResponsesTransport(
        premature,
        clock=lambda: NOW,
    )
    with pytest.raises(LocalCompatibleTransportError) as done_error:
        asyncio.run(_collect(transport, premature))
    assert done_error.value.code is LocalCompatibleTransportErrorCode.STREAM

    substituted = compiled_request().model_copy(update={"model": "other"})
    with pytest.raises(LocalCompatibleTransportError) as model_error:
        asyncio.run(
            _collect(
                transport,
                premature,
                compiled=substituted,
            )
        )
    assert model_error.value.code is LocalCompatibleTransportErrorCode.MODEL


def test_sse_line_and_truncation_are_bounded() -> None:
    oversized = BoundedSseFramer()
    with pytest.raises(LocalSseError) as size_error:
        oversized.feed(b"x" * (MAXIMUM_LOCAL_SSE_LINE_BYTES + 1))
    assert size_error.value.code is LocalSseErrorCode.LINE_SIZE

    truncated = BoundedSseFramer()
    truncated.feed(b"data: {\"partial\":true}")
    with pytest.raises(LocalSseError) as truncated_error:
        truncated.finish()
    assert truncated_error.value.code is LocalSseErrorCode.TRUNCATED


async def _collect(
    transport: BoundedLocalCompatibleResponsesTransport,
    connector: FakeStreamingConnector,
    *,
    cancel_after_first: bool = False,
    compiled=None,
):
    del connector
    cancellation = asyncio.Event()
    events = []
    async for event in transport.stream(
        local_request(),
        compiled or compiled_request(),
        authorized_route(),
        None,
        OpenAIResponsesDecoder(lambda *counts: 0),
        cancellation=cancellation,
        deadline_at=NOW + timedelta(seconds=30),
    ):
        events.append(event)
        if cancel_after_first and len(events) == 1:
            cancellation.set()
    return events


def _completed_sse_chunks() -> tuple[bytes, ...]:
    delta = _record(
        {
            "delta": "ok",
            "sequence_number": 0,
            "type": "response.output_text.delta",
        }
    )
    completed = _record(
        {
            "response": {
                "status": "completed",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                },
            },
            "sequence_number": 1,
            "type": "response.completed",
        }
    )
    body = b"data: " + delta + b"\n\ndata: " + completed + b"\n\n"
    midpoint = len(body) // 2
    return (body[:midpoint], body[midpoint:])


def _record(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
