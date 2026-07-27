import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import timedelta

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderCompleted,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderUsage,
)
from app.services.harness.providers.credential_material import CredentialLease
from app.services.harness.providers.egress_contracts import ProviderEgressRequest
from app.services.harness.providers.local_compatible_policy import (
    AuthorizedLocalCompatibleRoute,
)
from app.services.harness.providers.local_compatible_probe import (
    MAXIMUM_LOCAL_PROBE_RESPONSE_BYTES,
    LocalProbeCase,
)
from app.services.harness.providers.local_compatible_probe_backend import (
    LocalCompatibleProbeBackend,
)
from tests.harness.providers.local_compatible.fixtures import (
    MODEL_ID,
    NOW,
    authorized_route,
)


class RecordingConnector:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.requests: list[ProviderEgressRequest] = []
        self.closed = False

    def stream(
        self,
        request: ProviderEgressRequest,
        route: AuthorizedLocalCompatibleRoute,
        credential: CredentialLease | None,
        *,
        cancellation: asyncio.Event,
        deadline_at,
    ) -> AsyncGenerator[bytes, None]:
        del route, credential, cancellation, deadline_at
        self.requests.append(request)
        return self._chunks()

    async def _chunks(self) -> AsyncGenerator[bytes, None]:
        try:
            for chunk in self.chunks:
                yield chunk
        finally:
            self.closed = True


def test_base_probe_uses_production_decoder_and_bounded_wire_request() -> None:
    connector = RecordingConnector(_completed_chunks())
    backend = _backend(connector)

    result = asyncio.run(
        backend.run_case(
            LocalProbeCase.BASE_STREAM,
            cancellation=asyncio.Event(),
            cancel_after_first_event=False,
            deadline_at=NOW + timedelta(seconds=5),
        )
    )

    assert result.passed
    assert tuple(type(event) for event in result.events) == (
        ProviderTextDelta,
        ProviderUsage,
        ProviderCompleted,
    )
    assert result.response_bytes == sum(map(len, connector.chunks))
    assert connector.closed
    request = connector.requests[0]
    body = json.loads(request.body())
    assert body["model"] == MODEL_ID
    assert body["stream"] is True
    assert body["store"] is False
    assert body["truncation"] == "disabled"


def test_cancellation_probe_closes_stream_and_emits_terminal_cancel() -> None:
    connector = RecordingConnector(_completed_chunks())
    cancellation = asyncio.Event()

    result = asyncio.run(
        _backend(connector).run_case(
            LocalProbeCase.CANCELLATION,
            cancellation=cancellation,
            cancel_after_first_event=True,
            deadline_at=NOW + timedelta(seconds=5),
        )
    )

    assert result.passed
    assert result.transport_cancelled
    assert isinstance(result.events[-1], ProviderCancelled)
    assert cancellation.is_set()
    assert connector.closed


def test_tool_probe_sends_strict_tools_and_normalizes_both_calls() -> None:
    connector = RecordingConnector((_tool_sse(),))

    result = asyncio.run(
        _backend(connector).run_case(
            LocalProbeCase.TOOLS,
            cancellation=asyncio.Event(),
            cancel_after_first_event=False,
            deadline_at=NOW + timedelta(seconds=5),
        )
    )

    tool_calls = tuple(
        event
        for event in result.events
        if isinstance(event, ProviderToolCall)
    )
    assert tuple(call.tool_name for call in tool_calls) == (
        "probe_one",
        "probe_two",
    )
    body = json.loads(connector.requests[0].body())
    assert len(body["tools"]) == 2
    assert all(tool["strict"] is True for tool in body["tools"])
    assert all(
        tool["parameters"]["additionalProperties"] is False
        for tool in body["tools"]
    )


def test_reasoning_probe_adds_explicit_low_effort_contract() -> None:
    connector = RecordingConnector(_completed_chunks())

    asyncio.run(
        _backend(connector).run_case(
            LocalProbeCase.REASONING,
            cancellation=asyncio.Event(),
            cancel_after_first_event=False,
            deadline_at=NOW + timedelta(seconds=5),
        )
    )

    body = json.loads(connector.requests[0].body())
    assert body["reasoning"] == {"effort": "low", "summary": "auto"}


def test_malformed_or_oversized_stream_becomes_empty_evidence() -> None:
    malformed = RecordingConnector((b"data: {}\n\n",))
    malformed_result = asyncio.run(
        _backend(malformed).run_case(
            LocalProbeCase.BASE_STREAM,
            cancellation=asyncio.Event(),
            cancel_after_first_event=False,
            deadline_at=NOW + timedelta(seconds=5),
        )
    )
    assert not malformed_result.passed
    assert malformed_result.events == ()
    assert malformed_result.response_bytes == 0
    assert malformed.closed

    oversized = RecordingConnector(
        (b"x" * (MAXIMUM_LOCAL_PROBE_RESPONSE_BYTES + 1),)
    )
    oversized_result = asyncio.run(
        _backend(oversized).run_case(
            LocalProbeCase.BASE_STREAM,
            cancellation=asyncio.Event(),
            cancel_after_first_event=False,
            deadline_at=NOW + timedelta(seconds=5),
        )
    )
    assert not oversized_result.passed
    assert oversized_result.response_bytes == 0
    assert oversized.closed


def _backend(
    connector: RecordingConnector,
) -> LocalCompatibleProbeBackend:
    return LocalCompatibleProbeBackend(
        connector,
        authorized_route(),
        None,
        clock=lambda: NOW,
    )


def _completed_chunks() -> tuple[bytes, ...]:
    return (
        _sse(
            {
                "delta": "ready",
                "sequence_number": 0,
                "type": "response.output_text.delta",
            }
        ),
        _sse(
            {
                "response": {
                    "status": "completed",
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                    },
                },
                "sequence_number": 1,
                "type": "response.completed",
            }
        ),
    )


def _tool_sse() -> bytes:
    records = (
        {
            "item": {
                "call_id": "call_one",
                "id": "item_one",
                "name": "probe_one",
                "type": "function_call",
            },
            "sequence_number": 0,
            "type": "response.output_item.added",
        },
        {
            "arguments": "{}",
            "item_id": "item_one",
            "name": "probe_one",
            "sequence_number": 1,
            "type": "response.function_call_arguments.done",
        },
        {
            "item": {
                "call_id": "call_two",
                "id": "item_two",
                "name": "probe_two",
                "type": "function_call",
            },
            "sequence_number": 2,
            "type": "response.output_item.added",
        },
        {
            "arguments": "{}",
            "item_id": "item_two",
            "name": "probe_two",
            "sequence_number": 3,
            "type": "response.function_call_arguments.done",
        },
        {
            "response": {
                "status": "completed",
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": 2,
                },
            },
            "sequence_number": 4,
            "type": "response.completed",
        },
    )
    return b"".join(_sse(record) for record in records)


def _sse(record: dict[str, object]) -> bytes:
    encoded = json.dumps(
        record,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return b"data: " + encoded + b"\n\n"
