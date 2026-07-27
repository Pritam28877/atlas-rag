"""Fail-closed hooks and bounded plugin-host tests."""

import asyncio
import hashlib
import json
from collections.abc import Mapping

import pytest

from app.services.harness.extensions import (
    HookDescriptor,
    HookDispatcher,
    HookDispatchError,
    HookDispatchErrorCode,
    HookEvent,
    HookFailurePolicy,
    HookLifecycleEvent,
    HookMode,
    HookOutcome,
    HookRegistration,
    HookResponse,
    PluginCall,
    PluginDescriptor,
    PluginHost,
    PluginHostError,
    PluginHostErrorCode,
)

EVENT_ID = "evt_" + "1" * 32
REQUEST_ID = "req_" + "2" * 32


def _event() -> HookEvent:
    return HookEvent(
        event_id=EVENT_ID,
        event_type=HookLifecycleEvent.BEFORE_OPERATION,
        payload_json="{}",
    )


class FakeHookTransport:
    def __init__(self, response: object = None, failure: bool = False) -> None:
        self.response = response
        self.failure = failure
        self.closed = False

    async def invoke(
        self,
        event: HookEvent,
    ) -> bytes | str | Mapping[str, object]:
        if self.failure:
            raise RuntimeError("untrusted hook failure")
        if self.response is None:
            return HookResponse(
                event_id=event.event_id,
                outcome=HookOutcome.ALLOW,
                reason="accepted",
            ).model_dump()
        return self.response

    async def close(self) -> None:
        self.closed = True


def _descriptor(name: str, mode: HookMode) -> HookDescriptor:
    return HookDescriptor(
        name=name,
        version="1.0.0",
        mode=mode,
        events=(HookLifecycleEvent.BEFORE_OPERATION,),
        failure_policy=(
            HookFailurePolicy.FAIL_CLOSED
            if mode is HookMode.SECURITY
            else HookFailurePolicy.CONTINUE
        ),
    )


def test_security_hook_deny_and_abstain_never_allow_execution() -> None:
    async def scenario() -> None:
        deny = FakeHookTransport(
            HookResponse(
                event_id=EVENT_ID,
                outcome=HookOutcome.DENY,
                reason="policy denied",
            ).model_dump()
        )
        dispatcher = HookDispatcher(
            (
                HookRegistration(_descriptor("deny", HookMode.SECURITY), deny),
            )
        )
        result = await dispatcher.dispatch(_event())
        assert not result.allowed
        assert result.security_outcomes == (HookOutcome.DENY,)
        await dispatcher.close()
        assert deny.closed

    asyncio.run(scenario())


def test_security_failure_fails_closed_and_advisory_denial_cannot_mutate() -> None:
    async def scenario() -> None:
        security = HookDispatcher(
            (
                HookRegistration(
                    _descriptor("security", HookMode.SECURITY),
                    FakeHookTransport(failure=True),
                ),
            )
        )
        with pytest.raises(HookDispatchError) as failure:
            await security.dispatch(_event())
        assert failure.value.code is HookDispatchErrorCode.SECURITY_FAILURE

        advisory = HookDispatcher(
            (
                HookRegistration(
                    _descriptor("advisory", HookMode.ADVISORY),
                    FakeHookTransport(
                        HookResponse(
                            event_id=EVENT_ID,
                            outcome=HookOutcome.DENY,
                            reason="suggestion",
                        ).model_dump()
                    ),
                ),
            )
        )
        result = await advisory.dispatch(_event())
        assert result.allowed
        assert result.advisory_failures == ("advisory",)

    asyncio.run(scenario())


class FakePluginTransport:
    def __init__(self, response: object, *, delay: float = 0.0) -> None:
        self.response = response
        self.delay = delay
        self.closed = False

    async def invoke(
        self,
        call: PluginCall,
    ) -> bytes | str | Mapping[str, object]:
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.response

    async def close(self) -> None:
        self.closed = True


def _plugin_result() -> dict[str, object]:
    output = '{"ok":true}'
    return {
        "request_id": REQUEST_ID,
        "output_json": output,
        "output_bytes": len(output.encode()),
        "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
    }


def _plugin_call(capability: str = "files.read") -> PluginCall:
    return PluginCall(
        request_id=REQUEST_ID,
        method="inspect",
        capability=capability,
        arguments_json="{}",
        timeout_ms=100,
    )


def test_plugin_host_bounds_capabilities_and_cleanup() -> None:
    async def scenario() -> None:
        transport = FakePluginTransport(_plugin_result())
        host = PluginHost(
            PluginDescriptor(
                name="indexer",
                version="1.0.0",
                capabilities=("files.read",),
            ),
            transport,
        )
        result = await host.call(_plugin_call())
        assert result.output_json == '{"ok":true}'
        with pytest.raises(PluginHostError) as denied:
            await host.call(_plugin_call("network.open"))
        assert denied.value.code is PluginHostErrorCode.CAPABILITY_DENIED
        await host.close()
        assert transport.closed

    asyncio.run(scenario())


def test_plugin_host_rejects_hung_and_malformed_outputs() -> None:
    async def scenario() -> None:
        timeout_host = PluginHost(
            PluginDescriptor(
                name="indexer",
                version="1.0.0",
                capabilities=("files.read",),
            ),
            FakePluginTransport(_plugin_result(), delay=1),
        )
        with pytest.raises(PluginHostError) as timeout:
            await timeout_host.call(_plugin_call())
        assert timeout.value.code is PluginHostErrorCode.TIMEOUT
        await timeout_host.close()

        malformed_host = PluginHost(
            PluginDescriptor(
                name="indexer",
                version="1.0.0",
                capabilities=("files.read",),
            ),
            FakePluginTransport(json.dumps({"request_id": REQUEST_ID})),
        )
        with pytest.raises(PluginHostError) as malformed:
            await malformed_host.call(_plugin_call())
        assert malformed.value.code is PluginHostErrorCode.MALFORMED_RESULT
        await malformed_host.close()

    asyncio.run(scenario())
