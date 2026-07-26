#!/usr/bin/env python3
"""Exercise provider retry and cost controls without live provider traffic."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from app.services.harness.journal import (
    GlobalJournalReadRequest,
    JournalProviderEgressAuditSink,
    SQLiteEventJournal,
    SQLiteProviderCostLedger,
)
from app.services.harness.providers import (
    ConfiguredCredentialBroker,
    DeterministicProviderPayloadInspector,
    EnvironmentCredentialBackend,
    EnvironmentCredentialReference,
    ProviderDispatchCoordinator,
    ProviderEgressGateway,
    ProviderEgressPolicy,
    ProviderPayloadInspectionPolicy,
    SystemProviderAddressResolver,
    load_provider_configuration,
)
from scripts.harness_provider_dispatch_drill_support import (
    AdvancingRetryDelay,
    MutableClock,
    ScenarioConnector,
    call_counts,
    dispatch_scenario,
    egress_request,
    failed_dispatch_scenario,
    settled_cost,
)
from scripts.harness_provider_egress_drill_support import public_lookup

WORKSPACE_ID = "wsp_" + "1" * 32
ACTOR_ID = "prn_" + "2" * 32
RETRY_REQUEST_ID = "req_" + "3" * 32
AMBIGUOUS_REQUEST_ID = "req_" + "4" * 32
CAPPED_REQUEST_ID = "req_" + "5" * 32
RETRY_TURN_ID = "trn_" + "6" * 32
AMBIGUOUS_TURN_ID = "trn_" + "7" * 32
CAPPED_TURN_ID = "trn_" + "8" * 32


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
    route = next(route for route in loaded.configuration.routes if route.enabled)
    data_policy = next(
        policy
        for policy in loaded.configuration.data_policies
        if (
            policy.provider == route.provider
            and policy.policy_revision_sha256 == route.policy_revision_sha256
        )
    )
    clock = MutableClock(datetime(2026, 7, 27, 18, 0, tzinfo=UTC))
    credentials = ConfiguredCredentialBroker(
        loaded,
        EnvironmentCredentialBackend(
            (
                EnvironmentCredentialReference(
                    handle=route.credential_handle,
                    environment_variable=environment_variable,
                    lease_ttl_seconds=60,
                ),
            ),
            development_mode=True,
            clock=clock,
        ),
        clock=clock,
    )
    policy = ProviderEgressPolicy(
        provider=route.provider,
        destination_url=destination_url,
        destination_sha256=data_policy.destination_sha256,
        allowed_redirect_origins=(),
        accepted_classifications=data_policy.accepted_classifications,
        max_request_bytes=4_096,
        max_response_bytes=4_096,
    )
    connector = ScenarioConnector(
        {
            RETRY_REQUEST_ID: (503, 200),
            AMBIGUOUS_REQUEST_ID: (None,),
            CAPPED_REQUEST_ID: (503,),
        }
    )
    delay = AdvancingRetryDelay(clock)
    journal = await SQLiteEventJournal.open(database_path)
    ledger = await SQLiteProviderCostLedger.open(database_path)
    requests = {
        RETRY_REQUEST_ID: egress_request(
            RETRY_REQUEST_ID,
            route.provider,
            route.credential_handle,
            target_url,
            prompt_canary,
        ),
        AMBIGUOUS_REQUEST_ID: egress_request(
            AMBIGUOUS_REQUEST_ID,
            route.provider,
            route.credential_handle,
            target_url,
            prompt_canary,
        ),
        CAPPED_REQUEST_ID: egress_request(
            CAPPED_REQUEST_ID,
            route.provider,
            route.credential_handle,
            target_url,
            prompt_canary,
        ),
    }
    audit = JournalProviderEgressAuditSink(
        journal,
        workspace_id=WORKSPACE_ID,
        actor_principal_id=ACTOR_ID,
    )
    gateway = ProviderEgressGateway(
        credentials=credentials,
        resolver=SystemProviderAddressResolver(
            lookup=public_lookup,
            maximum_concurrent_lookups=2,
        ),
        inspector=DeterministicProviderPayloadInspector(
            ProviderPayloadInspectionPolicy(
                policy_revision_sha256=route.policy_revision_sha256,
                denied_body_sha256s=(),
                maximum_scan_bytes=4_096,
            )
        ),
        connector=connector,
        audit=audit,
        clock=clock,
    )
    coordinator = ProviderDispatchCoordinator(ledger, delay, clock=clock)
    try:
        retry_response = await dispatch_scenario(
            coordinator,
            gateway,
            requests[RETRY_REQUEST_ID],
            policy,
            RETRY_TURN_ID,
            clock,
            max_turn_cost=100,
        )
        ambiguous_code = await failed_dispatch_scenario(
            coordinator,
            gateway,
            requests[AMBIGUOUS_REQUEST_ID],
            policy,
            AMBIGUOUS_TURN_ID,
            clock,
            max_turn_cost=50,
        )
        capped_code = await failed_dispatch_scenario(
            coordinator,
            gateway,
            requests[CAPPED_REQUEST_ID],
            policy,
            CAPPED_TURN_ID,
            clock,
            max_turn_cost=50,
        )
    finally:
        await ledger.close()
        await journal.close()

    restarted_journal = await SQLiteEventJournal.open(database_path)
    restarted_ledger = await SQLiteProviderCostLedger.open(database_path)
    restarted_coordinator = ProviderDispatchCoordinator(
        restarted_ledger,
        delay,
        clock=clock,
    )
    calls_before_replay = len(connector.calls)
    try:
        replay_code = await failed_dispatch_scenario(
            restarted_coordinator,
            gateway,
            requests[RETRY_REQUEST_ID],
            policy,
            RETRY_TURN_ID,
            clock,
            max_turn_cost=100,
        )
        snapshots = {
            "retry": await settled_cost(
                restarted_ledger, RETRY_TURN_ID, clock
            ),
            "ambiguous": await settled_cost(
                restarted_ledger, AMBIGUOUS_TURN_ID, clock
            ),
            "capped": await settled_cost(
                restarted_ledger, CAPPED_TURN_ID, clock
            ),
        }
        audit_page = await restarted_journal.read_global(
            GlobalJournalReadRequest(
                workspace_id=WORKSPACE_ID,
                after_journal_sequence=0,
                limit=32,
            )
        )
    finally:
        await restarted_ledger.close()
        await restarted_journal.close()

    persisted = database_path.read_bytes()
    secret = os.environb[environment_variable.encode("ascii")]
    return {
        "active_leases": await credentials.active_leases(),
        "ambiguous_error": ambiguous_code,
        "audit_events": len(audit_page.events),
        "call_counts": call_counts(connector.calls),
        "capped_error": capped_code,
        "costs_after_restart": snapshots,
        "credential_present": all(connector.credential_present),
        "database_secret_free": (
            secret not in persisted
            and prompt_canary.encode() not in persisted
        ),
        "delays_ms": delay.delays_ms,
        "replay_error": replay_code,
        "replay_redispatched": len(connector.calls) != calls_before_replay,
        "response_status": retry_response.status,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-path", required=True, type=Path)
    parser.add_argument("--database-path", required=True, type=Path)
    parser.add_argument("--destination-url", required=True)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--environment-variable", required=True)
    parser.add_argument("--prompt-canary", required=True)
    report = asyncio.run(run_probe(**vars(parser.parse_args())))
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
