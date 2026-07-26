import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.services.harness.protocol import (
    ProviderCompleted,
    ProviderTextDelta,
    ProviderUsage,
)
from app.services.harness.providers import (
    AuthorizedBedrockRoute,
    BedrockConverseStreamDecoder,
    BedrockCredentialMaterial,
    BedrockInferenceConfiguration,
    BedrockMessage,
    BedrockTextContent,
    BedrockTransportError,
    BedrockTransportErrorCode,
    BoundedBedrockConverseTransport,
    CompiledBedrockConverseStreamRequest,
)
from app.services.harness.providers.egress_policy import (
    provider_destination_sha256,
)

ENDPOINT = "https://bedrock-runtime.us-east-1.amazonaws.com/"


class FakeStream:
    def __init__(self, events: tuple[dict[str, object], ...]) -> None:
        self._events = iter(events)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self) -> dict[str, object]:
        return next(self._events)

    def close(self) -> None:
        self.closed = True


class BlockingFakeStream(FakeStream):
    def __init__(self) -> None:
        super().__init__(())
        self._event_index = 0
        self.entered_blocking_read = threading.Event()
        self.release_blocking_read = threading.Event()

    def __next__(self) -> dict[str, object]:
        events = _happy_events()
        if self._event_index < 2:
            event = events[self._event_index]
            self._event_index += 1
            return event
        self.entered_blocking_read.set()
        self.release_blocking_read.wait(timeout=2)
        raise StopIteration


class FakeClient:
    def __init__(
        self,
        events: tuple[dict[str, object], ...],
        *,
        fail: bool = False,
    ) -> None:
        self.stream = FakeStream(events)
        self.fail = fail
        self.closed = False
        self.request: dict[str, object] | None = None

    def converse_stream(self, **request: object) -> dict[str, object]:
        self.request = request
        if self.fail:
            raise RuntimeError("provider detail must not escape")
        return {
            "stream": self.stream,
            "ResponseMetadata": {"RequestId": "safe"},
        }

    def close(self) -> None:
        self.closed = True


class FakeFactory:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.timeouts: list[int] = []

    def create(
        self,
        route: AuthorizedBedrockRoute,
        credential: BedrockCredentialMaterial,
        *,
        timeout_seconds: int,
    ) -> FakeClient:
        assert route == _route()
        assert bytes(credential.views()[0]) == b"access"
        self.timeouts.append(timeout_seconds)
        return self.client


def test_transport_streams_canonical_events_and_cleans_resources() -> None:
    async def scenario() -> None:
        client = FakeClient(_happy_events())
        factory = FakeFactory(client)
        transport = BoundedBedrockConverseTransport(
            factory,
            clock=lambda: datetime.now(UTC),
            maximum_concurrent_streams=1,
        )
        credential = _credential()
        decoder = BedrockConverseStreamDecoder(lambda *counts: 7)

        events = [
            event
            async for event in transport.stream(
                _request(),
                _route(),
                credential,
                decoder,
                cancellation=asyncio.Event(),
                deadline_at=datetime.now(UTC) + timedelta(seconds=5),
            )
        ]

        assert isinstance(events[0], ProviderTextDelta)
        assert isinstance(events[1], ProviderUsage)
        assert isinstance(events[2], ProviderCompleted)
        assert factory.timeouts == [5]
        assert client.request is not None
        assert client.request["modelId"] == "amazon.nova-lite-v1:0"
        assert client.stream.closed
        assert client.closed
        assert bytes(credential.views()[0]) == bytes(6)
        assert decoder.metadata is not None
        assert decoder.metadata.provider_metadata_sha256 is not None
        await transport.close()

    asyncio.run(scenario())


def test_provider_failure_is_redacted_and_cancel_releases_credential() -> None:
    async def scenario() -> None:
        failed_transport = BoundedBedrockConverseTransport(
            FakeFactory(FakeClient((), fail=True)),
            clock=lambda: datetime.now(UTC),
        )
        with pytest.raises(BedrockTransportError) as failed:
            async for _event in failed_transport.stream(
                _request(),
                _route(),
                _credential(),
                BedrockConverseStreamDecoder(lambda *counts: 0),
                cancellation=asyncio.Event(),
                deadline_at=datetime.now(UTC) + timedelta(seconds=5),
            ):
                pass
        assert failed.value.code is BedrockTransportErrorCode.PROVIDER
        assert "provider detail" not in str(failed.value)
        await failed_transport.close()

        cancelled_transport = BoundedBedrockConverseTransport(
            FakeFactory(FakeClient(_happy_events())),
            clock=lambda: datetime.now(UTC),
        )
        cancellation = asyncio.Event()
        cancellation.set()
        credential = _credential()
        with pytest.raises(BedrockTransportError) as cancelled:
            async for _event in cancelled_transport.stream(
                _request(),
                _route(),
                credential,
                BedrockConverseStreamDecoder(lambda *counts: 0),
                cancellation=cancellation,
                deadline_at=datetime.now(UTC) + timedelta(seconds=5),
            ):
                pass
        assert cancelled.value.code is BedrockTransportErrorCode.CANCELLED
        await cancelled_transport.close()
        assert bytes(credential.views()[0]) == bytes(6)

    asyncio.run(scenario())


def test_rejected_requests_always_release_credentials() -> None:
    async def scenario() -> None:
        transport = BoundedBedrockConverseTransport(
            FakeFactory(FakeClient(())),
            clock=lambda: datetime.now(UTC),
        )
        await transport.close()
        closed_credential = _credential()
        with pytest.raises(BedrockTransportError) as closed:
            async for _event in transport.stream(
                _request(),
                _route(),
                closed_credential,
                BedrockConverseStreamDecoder(lambda *counts: 0),
                cancellation=asyncio.Event(),
                deadline_at=datetime.now(UTC) + timedelta(seconds=5),
            ):
                pass
        assert closed.value.code is BedrockTransportErrorCode.CLOSED
        assert bytes(closed_credential.views()[0]) == bytes(6)

        expired_transport = BoundedBedrockConverseTransport(
            FakeFactory(FakeClient(())),
            clock=lambda: datetime.now(UTC),
        )
        expired_credential = _credential()
        with pytest.raises(BedrockTransportError) as expired:
            async for _event in expired_transport.stream(
                _request(),
                _route(),
                expired_credential,
                BedrockConverseStreamDecoder(lambda *counts: 0),
                cancellation=asyncio.Event(),
                deadline_at=datetime.now(UTC) - timedelta(seconds=1),
            ):
                pass
        assert expired.value.code is BedrockTransportErrorCode.DEADLINE
        assert bytes(expired_credential.views()[0]) == bytes(6)
        await expired_transport.close()

    asyncio.run(scenario())


def test_capacity_remains_reserved_until_blocking_worker_exits() -> None:
    async def scenario() -> None:
        client = FakeClient(())
        blocking_stream = BlockingFakeStream()
        client.stream = blocking_stream
        transport = BoundedBedrockConverseTransport(
            FakeFactory(client),
            clock=lambda: datetime.now(UTC),
            maximum_concurrent_streams=1,
        )
        first_credential = _credential()
        iterator = transport.stream(
            _request(),
            _route(),
            first_credential,
            BedrockConverseStreamDecoder(lambda *counts: 0),
            cancellation=asyncio.Event(),
            deadline_at=datetime.now(UTC) + timedelta(seconds=5),
        ).__aiter__()

        assert isinstance(await anext(iterator), ProviderTextDelta)
        assert await asyncio.to_thread(
            blocking_stream.entered_blocking_read.wait,
            1,
        )
        await iterator.aclose()

        queued_credential = _credential()
        with pytest.raises(BedrockTransportError) as blocked:
            async for _event in transport.stream(
                _request(),
                _route(),
                queued_credential,
                BedrockConverseStreamDecoder(lambda *counts: 0),
                cancellation=asyncio.Event(),
                deadline_at=datetime.now(UTC) + timedelta(milliseconds=50),
            ):
                pass
        assert blocked.value.code is BedrockTransportErrorCode.DEADLINE
        assert bytes(queued_credential.views()[0]) == bytes(6)

        blocking_stream.release_blocking_read.set()
        await transport.close()
        assert bytes(first_credential.views()[0]) == bytes(6)

    asyncio.run(scenario())


def _request() -> CompiledBedrockConverseStreamRequest:
    return CompiledBedrockConverseStreamRequest(
        model_id="amazon.nova-lite-v1:0",
        messages=(
            BedrockMessage(
                role="user",
                content=(BedrockTextContent(text="hello"),),
            ),
        ),
        system=(),
        inference_config=BedrockInferenceConfiguration(max_tokens=16),
        tools=(),
    )


def _route() -> AuthorizedBedrockRoute:
    return AuthorizedBedrockRoute(
        route_id="bedrock.primary",
        model_id="amazon.nova-lite-v1:0",
        region="us-east-1",
        credential_handle="pcr_" + "2" * 32,
        identity_reference_id="awsid_" + "1" * 32,
        endpoint_url=ENDPOINT,
        destination_sha256=provider_destination_sha256(ENDPOINT),
        allowed_inference_regions=("us-east-1",),
        cross_region_inference=False,
    )


def _credential() -> BedrockCredentialMaterial:
    return BedrockCredentialMaterial(
        bytearray(b"access"),
        bytearray(b"secret"),
        bytearray(b"token"),
        expires_at=None,
    )


def _happy_events() -> tuple[dict[str, object], ...]:
    return (
        {"messageStart": {"role": "assistant"}},
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"text": "hello"},
            }
        },
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {
            "metadata": {
                "metrics": {"latencyMs": 1},
                "usage": {
                    "inputTokens": 1,
                    "outputTokens": 1,
                    "totalTokens": 2,
                },
            }
        },
    )
