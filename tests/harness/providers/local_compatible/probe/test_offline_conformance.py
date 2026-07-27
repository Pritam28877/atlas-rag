import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import timedelta

from app.services.harness.providers.local_compatible_capabilities import (
    LocalCompatibleFeature,
)
from app.services.harness.providers.local_compatible_probe import (
    BoundedLocalCompatibleProbeRunner,
)
from app.services.harness.providers.local_compatible_probe_backend import (
    LocalCompatibleProbeBackend,
)
from tests.harness.providers.local_compatible.fixtures import (
    NOW,
    authorized_route,
    local_model,
)


class QueuedConnector:
    def __init__(self, streams: tuple[tuple[bytes, ...], ...]) -> None:
        self._streams = iter(streams)
        self.request_count = 0
        self.closed_streams = 0

    def stream(self, *args, **kwargs) -> AsyncGenerator[bytes, None]:
        del args, kwargs
        self.request_count += 1
        return self._stream(next(self._streams))

    async def _stream(
        self,
        chunks: tuple[bytes, ...],
    ) -> AsyncGenerator[bytes, None]:
        try:
            for chunk in chunks:
                yield chunk
        finally:
            self.closed_streams += 1


def test_six_cases_produce_bound_capability_evidence() -> None:
    connector = QueuedConnector(
        (
            _text_completion(),
            _text_completion(),
            _text_completion(),
            _completion(cached_tokens=1),
            _reasoning_completion(),
            (_tool_completion(),),
        )
    )
    route = authorized_route()
    backend = LocalCompatibleProbeBackend(
        connector,
        route,
        None,
        clock=lambda: NOW,
    )
    runner = BoundedLocalCompatibleProbeRunner(
        backend,
        clock=lambda: NOW,
    )

    probe = asyncio.run(
        runner.probe(
            route,
            model_revision_sha256=local_model().model_revision_sha256,
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=30),
        )
    )

    assert set(probe.supported_features) == set(LocalCompatibleFeature)
    assert probe.destination_sha256 == route.destination_sha256
    assert connector.request_count == 6
    assert connector.closed_streams == 6
    assert "ready" not in probe.model_dump_json()


def _text_completion() -> tuple[bytes, ...]:
    return (
        _sse(
            {
                "delta": "ready",
                "sequence_number": 0,
                "type": "response.output_text.delta",
            }
        ),
        *_completion(sequence=1),
    )


def _completion(
    *,
    cached_tokens: int = 0,
    sequence: int = 0,
) -> tuple[bytes, ...]:
    usage: dict[str, object] = {
        "input_tokens": 2,
        "output_tokens": 1,
    }
    if cached_tokens:
        usage["input_tokens_details"] = {
            "cached_tokens": cached_tokens,
        }
    return (
        _sse(
            {
                "response": {
                    "status": "completed",
                    "usage": usage,
                },
                "sequence_number": sequence,
                "type": "response.completed",
            }
        ),
    )


def _reasoning_completion() -> tuple[bytes, ...]:
    return (
        _sse(
            {
                "delta": "reasoning",
                "sequence_number": 0,
                "type": "response.reasoning_summary_text.delta",
            }
        ),
        _sse(
            {
                "response": {
                    "status": "completed",
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 2,
                        "output_tokens_details": {
                            "reasoning_tokens": 1,
                        },
                    },
                },
                "sequence_number": 1,
                "type": "response.completed",
            }
        ),
    )


def _tool_completion() -> bytes:
    records = (
        _tool_added(0, "one"),
        _tool_done(1, "one"),
        _tool_added(2, "two"),
        _tool_done(3, "two"),
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


def _tool_added(sequence: int, suffix: str) -> dict[str, object]:
    return {
        "item": {
            "call_id": f"call_{suffix}",
            "id": f"item_{suffix}",
            "name": f"probe_{suffix}",
            "type": "function_call",
        },
        "sequence_number": sequence,
        "type": "response.output_item.added",
    }


def _tool_done(sequence: int, suffix: str) -> dict[str, object]:
    return {
        "arguments": "{}",
        "item_id": f"item_{suffix}",
        "name": f"probe_{suffix}",
        "sequence_number": sequence,
        "type": "response.function_call_arguments.done",
    }


def _sse(record: dict[str, object]) -> bytes:
    encoded = json.dumps(
        record,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return b"data: " + encoded + b"\n\n"
