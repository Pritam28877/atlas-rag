#!/usr/bin/env python3
"""Exercise provider egress end to end without opening a network socket."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services.harness.journal import (
    GlobalJournalReadRequest,
    JournalProviderEgressAuditSink,
    SQLiteEventJournal,
)
from app.services.harness.protocol import DataClassification
from app.services.harness.providers import (
    ConfiguredCredentialBroker,
    DeterministicProviderPayloadInspector,
    EnvironmentCredentialBackend,
    EnvironmentCredentialReference,
    HttpCoreEgressConnector,
    PinnedProviderNetworkBackend,
    ProviderEgressGateway,
    ProviderEgressGatewayError,
    ProviderEgressGatewayErrorCode,
    ProviderEgressPolicy,
    ProviderEgressPolicyError,
    ProviderEgressPolicyErrorCode,
    ProviderEgressRequest,
    ProviderPayloadInspectionPolicy,
    SystemProviderAddressResolver,
    load_provider_configuration,
)
from scripts.harness_provider_egress_drill_support import (
    BearerCredentialEncoder,
    RecordingBackend,
    private_lookup,
    public_lookup,
)


async def run_probe(
    *,
    config_path: Path,
    database_path: Path,
    destination_url: str,
    target_url: str,
    environment_variable: str,
    prompt_canary: str,
) -> dict[str, object]:
    loaded = await load_provider_configuration(config_path)
    route = next(
        route
        for route in loaded.configuration.routes
        if route.enabled
    )
    data_policy = next(
        policy
        for policy in loaded.configuration.data_policies
        if (
            policy.provider == route.provider
            and policy.policy_revision_sha256
            == route.policy_revision_sha256
        )
    )
    def clock() -> datetime:
        return datetime.now(UTC)

    credential_backend = EnvironmentCredentialBackend(
        (
            EnvironmentCredentialReference(
                handle=route.credential_handle,
                environment_variable=environment_variable,
                lease_ttl_seconds=60,
            ),
        ),
        development_mode=True,
        clock=clock,
    )
    credential_broker = ConfiguredCredentialBroker(
        loaded,
        credential_backend,
        clock=clock,
    )
    inspector = DeterministicProviderPayloadInspector(
        ProviderPayloadInspectionPolicy(
            policy_revision_sha256=route.policy_revision_sha256,
            denied_body_sha256s=(),
            maximum_scan_bytes=4_096,
        )
    )
    public_resolver = SystemProviderAddressResolver(
        lookup=public_lookup,
        maximum_concurrent_lookups=2,
    )
    recording_backend = RecordingBackend()
    encoder = BearerCredentialEncoder()
    connector = HttpCoreEgressConnector(
        encoder,
        backend_factory=lambda target: PinnedProviderNetworkBackend(
            target,
            backend=recording_backend,
        ),
    )
    egress_policy = ProviderEgressPolicy(
        provider=route.provider,
        destination_url=destination_url,
        destination_sha256=data_policy.destination_sha256,
        allowed_redirect_origins=(),
        accepted_classifications=data_policy.accepted_classifications,
        max_request_bytes=4_096,
        max_response_bytes=4_096,
    )
    journal = await SQLiteEventJournal.open(database_path)
    try:
        audit = JournalProviderEgressAuditSink(
            journal,
            workspace_id="wsp_" + "1" * 32,
            actor_principal_id="prn_" + "2" * 32,
        )
        gateway = ProviderEgressGateway(
            credentials=credential_broker,
            resolver=public_resolver,
            inspector=inspector,
            connector=connector,
            audit=audit,
            clock=clock,
        )
        request = _request(
            provider=route.provider,
            credential_handle=route.credential_handle,
            target_url=target_url,
            body=json.dumps(
                {"prompt": prompt_canary},
                separators=(",", ":"),
            ).encode(),
        )
        response = await gateway.send(
            request,
            egress_policy,
            cancellation=asyncio.Event(),
            deadline_at=clock() + timedelta(seconds=5),
            attempt=1,
        )
        transmitted = b"".join(recording_backend.stream.writes)
        authorization_transmitted = b"authorization:" in transmitted.lower()
        recording_backend.stream.writes.clear()
        del transmitted

        dlp_blocked = await _dlp_is_blocked(
            gateway,
            egress_policy,
            route.provider,
            route.credential_handle,
            target_url,
            clock,
        )
        private_address_blocked = await _private_address_is_blocked(
            credential_broker,
            inspector,
            connector,
            audit,
            egress_policy,
            request,
            clock,
        )
        audit_page = await journal.read_global(
            GlobalJournalReadRequest(
                workspace_id="wsp_" + "1" * 32,
                after_journal_sequence=0,
                limit=10,
            )
        )
    finally:
        await journal.close()

    persisted = database_path.read_bytes()
    prompt_bytes = prompt_canary.encode()
    environment_secret = os.environb[environment_variable.encode("ascii")]
    return {
        "active_leases": await credential_broker.active_leases(),
        "audit_event_types": [
            event.event.event_type for event in audit_page.events
        ],
        "audit_events": len(audit_page.events),
        "authorization_transmitted": authorization_transmitted,
        "dlp_blocked": dlp_blocked,
        "journal_secret_free": (
            prompt_bytes not in persisted
            and environment_secret not in persisted
        ),
        "network_calls": recording_backend.tcp_calls,
        "private_address_blocked": private_address_blocked,
        "response_status": response.status,
        "temporary_auth_zeroed": all(
            not any(buffer) for buffer in encoder.temporary_buffers
        ),
        "tls_server_hostname": recording_backend.stream.server_hostname,
    }


def _request(
    *,
    provider: str,
    credential_handle: str,
    target_url: str,
    body: bytes,
) -> ProviderEgressRequest:
    return ProviderEgressRequest(
        request_id="req_" + "3" * 32,
        provider=provider,
        credential_handle=credential_handle,
        target_url=target_url,
        classification=DataClassification.CONFIDENTIAL,
        content_type="application/json",
        safe_headers=(),
        body=body,
    )


async def _dlp_is_blocked(
    gateway: ProviderEgressGateway,
    policy: ProviderEgressPolicy,
    provider: str,
    credential_handle: str,
    target_url: str,
    clock: Callable[[], datetime],
) -> bool:
    request = _request(
        provider=provider,
        credential_handle=credential_handle,
        target_url=target_url,
        body=b'{"api_key":"must-not-leave"}',
    )
    try:
        await gateway.send(
            request,
            policy,
            cancellation=asyncio.Event(),
            deadline_at=clock() + timedelta(seconds=5),
            attempt=1,
        )
    except ProviderEgressGatewayError as error:
        return error.code is ProviderEgressGatewayErrorCode.INSPECTION
    return False


async def _private_address_is_blocked(
    credentials: ConfiguredCredentialBroker,
    inspector: DeterministicProviderPayloadInspector,
    connector: HttpCoreEgressConnector,
    audit: JournalProviderEgressAuditSink,
    policy: ProviderEgressPolicy,
    request: ProviderEgressRequest,
    clock: Callable[[], datetime],
) -> bool:
    gateway = ProviderEgressGateway(
        credentials=credentials,
        resolver=SystemProviderAddressResolver(lookup=private_lookup),
        inspector=inspector,
        connector=connector,
        audit=audit,
        clock=clock,
    )
    try:
        await gateway.send(
            request,
            policy,
            cancellation=asyncio.Event(),
            deadline_at=clock() + timedelta(seconds=5),
            attempt=1,
        )
    except ProviderEgressPolicyError as error:
        return error.code is ProviderEgressPolicyErrorCode.ADDRESS
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-path", required=True, type=Path)
    parser.add_argument("--database-path", required=True, type=Path)
    parser.add_argument("--destination-url", required=True)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--environment-variable", required=True)
    parser.add_argument("--prompt-canary", required=True)
    arguments = parser.parse_args()
    report = asyncio.run(run_probe(**vars(arguments)))
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
