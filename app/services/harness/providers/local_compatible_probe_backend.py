"""Production transport backend for bounded local capability probes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from datetime import datetime
from typing import Literal

from app.services.harness.protocol import (
    DataClassification,
    ProviderCancelled,
    ProviderStreamEvent,
)
from app.services.harness.providers.credential_material import CredentialLease
from app.services.harness.providers.egress_contracts import (
    ProviderEgressRequest,
    SafeEgressHeader,
    provider_egress_request_sha256,
)
from app.services.harness.providers.local_compatible_policy import (
    AuthorizedLocalCompatibleRoute,
)
from app.services.harness.providers.local_compatible_probe import (
    MAXIMUM_LOCAL_PROBE_EVENTS,
    MAXIMUM_LOCAL_PROBE_RESPONSE_BYTES,
    LocalProbeCase,
    LocalProbeCaseResult,
)
from app.services.harness.providers.local_compatible_stream_transport import (
    BoundedLocalCompatibleResponsesTransport,
    LocalCompatibleTransportError,
    LocalStreamingBody,
    LocalStreamingConnector,
)
from app.services.harness.providers.openai_contracts import (
    CompiledOpenAIResponsesRequest,
    OpenAIFunctionTool,
    OpenAIInputText,
    OpenAIMessageInput,
)
from app.services.harness.providers.openai_decoder import OpenAIResponsesDecoder

_PROBE_OUTPUT_TOKENS = 64
_PROMPT_CACHE_KEY = hashlib.sha256(b"atlas-local-probe-cache").hexdigest()


class LocalCompatibleProbeBackend:
    def __init__(
        self,
        connector: LocalStreamingConnector,
        route: AuthorizedLocalCompatibleRoute,
        credential: CredentialLease | None,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._connector = connector
        self._route = route
        self._credential = credential
        self._clock = clock

    async def run_case(
        self,
        case: LocalProbeCase,
        *,
        cancellation: asyncio.Event,
        cancel_after_first_event: bool,
        deadline_at: datetime,
    ) -> LocalProbeCaseResult:
        if cancel_after_first_event != (
            case is LocalProbeCase.CANCELLATION
        ):
            raise ValueError("local probe cancellation case is inconsistent")
        request = _probe_request(case, self._route)
        counting_connector = _CountingConnector(self._connector)
        transport = BoundedLocalCompatibleResponsesTransport(
            counting_connector,
            clock=self._clock,
            maximum_concurrent_streams=1,
        )
        events: list[ProviderStreamEvent] = []
        try:
            async for event in transport.stream_egress(
                request,
                self._route,
                self._credential,
                OpenAIResponsesDecoder(_zero_cost),
                cancellation=cancellation,
                deadline_at=deadline_at,
            ):
                events.append(event)
                if len(events) > MAXIMUM_LOCAL_PROBE_EVENTS:
                    return _failed_result(case, request)
                if cancel_after_first_event and not cancellation.is_set():
                    cancellation.set()
        except LocalCompatibleTransportError:
            return _failed_result(case, request)
        finally:
            await transport.close()
        event_tuple = tuple(events)
        transport_cancelled = (
            case is LocalProbeCase.CANCELLATION
            and bool(event_tuple)
            and isinstance(event_tuple[-1], ProviderCancelled)
        )
        if case is LocalProbeCase.CANCELLATION and not transport_cancelled:
            return _failed_result(case, request)
        return LocalProbeCaseResult(
            case=case,
            request_sha256=provider_egress_request_sha256(request),
            response_bytes=counting_connector.response_bytes,
            events=event_tuple,
            transport_cancelled=transport_cancelled,
            passed=bool(event_tuple),
        )


class _CountingConnector:
    def __init__(self, connector: LocalStreamingConnector) -> None:
        self._connector = connector
        self.response_bytes = 0

    def stream(
        self,
        request: ProviderEgressRequest,
        route: AuthorizedLocalCompatibleRoute,
        credential: CredentialLease | None,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> LocalStreamingBody:
        return self._stream(
            request,
            route,
            credential,
            cancellation=cancellation,
            deadline_at=deadline_at,
        )

    async def _stream(
        self,
        request: ProviderEgressRequest,
        route: AuthorizedLocalCompatibleRoute,
        credential: CredentialLease | None,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> AsyncGenerator[bytes, None]:
        stream = self._connector.stream(
            request,
            route,
            credential,
            cancellation=cancellation,
            deadline_at=deadline_at,
        )
        async with aclosing(stream):
            async for chunk in stream:
                self.response_bytes += len(chunk)
                if (
                    self.response_bytes
                    > MAXIMUM_LOCAL_PROBE_RESPONSE_BYTES
                ):
                    raise ValueError("local probe response limit exceeded")
                yield chunk


def _probe_request(
    case: LocalProbeCase,
    route: AuthorizedLocalCompatibleRoute,
) -> ProviderEgressRequest:
    compiled = _compiled_probe(case, route.model_id)
    payload = compiled.model_dump(mode="json", exclude_none=True)
    if case is LocalProbeCase.REASONING:
        payload["reasoning"] = {"effort": "low", "summary": "auto"}
    body = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    request_digest = hashlib.sha256(case.value.encode()).hexdigest()[:32]
    return ProviderEgressRequest(
        request_id=f"req_{request_digest}",
        provider="local-compatible",
        credential_handle=route.credential_handle,
        target_url=route.responses_url,
        classification=DataClassification.PUBLIC,
        content_type="application/json",
        safe_headers=(
            SafeEgressHeader(name="accept", value="text/event-stream"),
        ),
        body=body,
    )


def _compiled_probe(
    case: LocalProbeCase,
    model_id: str,
) -> CompiledOpenAIResponsesRequest:
    role: Literal["developer", "user"] = (
        "developer"
        if case is LocalProbeCase.DEVELOPER_ROLE
        else "user"
    )
    input_messages = (
        OpenAIMessageInput(
            role=role,
            content=(
                OpenAIInputText(text=_probe_prompt(case)),
            ),
        ),
    )
    tools = _probe_tools() if case is LocalProbeCase.TOOLS else ()
    cache_key = (
        _PROMPT_CACHE_KEY
        if case is LocalProbeCase.PROMPT_CACHE
        else None
    )
    return CompiledOpenAIResponsesRequest(
        model=model_id,
        input=input_messages,
        tools=tools,
        max_output_tokens=_PROBE_OUTPUT_TOKENS,
        prompt_cache_key=cache_key,
    )


def _probe_prompt(case: LocalProbeCase) -> str:
    if case is LocalProbeCase.TOOLS:
        return "Call probe_one and probe_two once each with no arguments."
    if case is LocalProbeCase.REASONING:
        return "Reply with the single word ready after reasoning."
    return "Reply with the single word ready."


def _probe_tools() -> tuple[OpenAIFunctionTool, ...]:
    parameters: dict[str, object] = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    return (
        OpenAIFunctionTool(
            name="probe_one",
            description="First compatibility probe.",
            parameters=parameters,
        ),
        OpenAIFunctionTool(
            name="probe_two",
            description="Second compatibility probe.",
            parameters=parameters,
        ),
    )


def _failed_result(
    case: LocalProbeCase,
    request: ProviderEgressRequest,
) -> LocalProbeCaseResult:
    return LocalProbeCaseResult(
        case=case,
        request_sha256=provider_egress_request_sha256(request),
        response_bytes=0,
        events=(),
        transport_cancelled=False,
        passed=False,
    )


def _zero_cost(
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
) -> int:
    del input_tokens, cached_tokens, output_tokens, reasoning_tokens
    return 0
