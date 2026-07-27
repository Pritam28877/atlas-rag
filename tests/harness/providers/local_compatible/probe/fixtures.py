"""Private local probe inputs and six compatible SSE streams."""

import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path

from app.cli.harness.local_probe_contracts import (
    LocalProbeLaunchRequest,
    authorize_local_probe,
)
from app.services.harness.protocol import DataClassification
from tests.harness.providers.local_compatible.fixtures import (
    MODEL_ID,
    configuration,
    identity,
    route_policy,
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


def authorized_probe(run_directory: Path):
    configuration_path = run_directory / "providers.json"
    route_path = run_directory / "route.json"
    identity_path = run_directory / "identity.json"
    loaded, _ = configuration()
    public_policy = loaded.configuration.data_policies[0].model_copy(
        update={
            "accepted_classifications": (DataClassification.PUBLIC,),
        }
    )
    probe_configuration = loaded.configuration.model_copy(
        update={"data_policies": (public_policy,)}
    )
    _write_private(
        configuration_path,
        probe_configuration.model_dump_json(),
    )
    _write_private(route_path, route_policy().model_dump_json())
    _write_private(identity_path, identity().model_dump_json())
    return authorize_local_probe(
        LocalProbeLaunchRequest(
            acknowledged=True,
            model=MODEL_ID,
            per_case_timeout_seconds=10,
            evidence_ttl_seconds=900,
            gate_environment_variable="ATLAS_LOCAL_PROBE_ENABLED",
            credential_environment_variable=None,
            configuration_path=configuration_path,
            route_policy_path=route_path,
            identity_path=identity_path,
            result_path=run_directory / "capabilities.json",
        )
    )


def compatible_streams() -> tuple[tuple[bytes, ...], ...]:
    return (
        _text_completion(),
        _text_completion(),
        _text_completion(),
        _completion(cached_tokens=1),
        _reasoning_completion(),
        (_tool_completion(),),
    )


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


def _write_private(path: Path, content: str) -> None:
    path.write_text(content)
    os.chmod(path, 0o600)
