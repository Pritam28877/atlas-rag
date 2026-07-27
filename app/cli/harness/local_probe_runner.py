"""Explicitly gated runtime composition for local capability evidence."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from app.cli.harness.adapter_smoke_runtime import ADAPTER_SMOKE_GATE_VALUE
from app.cli.harness.local_adapter_credentials import (
    acquire_local_adapter_credential,
)
from app.cli.harness.local_probe_contracts import AuthorizedLocalProbe
from app.cli.harness.provider_smoke_io import (
    read_private_smoke_input,
    validate_private_smoke_input,
    validate_provider_smoke_output_path,
    write_private_smoke_output,
)
from app.services.harness.protocol import (
    DataClassification,
    ProviderModelCapabilities,
)
from app.services.harness.providers import (
    ConfiguredCredentialBroker,
    LoadedProviderConfiguration,
    ProviderRouteConfiguration,
    SystemProviderAddressResolver,
    load_provider_configuration,
)
from app.services.harness.providers.credential_material import CredentialLease
from app.services.harness.providers.local_compatible_capabilities import (
    LocalCompatibleProbe,
)
from app.services.harness.providers.local_compatible_httpcore import (
    LocalCompatibleHttpCoreConnector,
)
from app.services.harness.providers.local_compatible_identity import (
    LocalCompatibleIdentityReference,
)
from app.services.harness.providers.local_compatible_policy import (
    LocalCompatibleRoutePolicy,
    authorize_local_compatible_route,
)
from app.services.harness.providers.local_compatible_probe import (
    BoundedLocalCompatibleProbeRunner,
)
from app.services.harness.providers.local_compatible_probe_backend import (
    LocalCompatibleProbeBackend,
)
from app.services.harness.providers.local_compatible_stream_transport import (
    LocalStreamingConnector,
)

Clock = Callable[[], datetime]
MAXIMUM_LOCAL_PROBE_INPUT_BYTES = 64 * 1024
LOCAL_PROBE_OUTPUT_TOKENS = 64


async def run_local_capability_probe(
    authorized: AuthorizedLocalProbe,
    *,
    connector: LocalStreamingConnector | None = None,
    environment: Mapping[bytes, bytes] | None = None,
    clock: Clock | None = None,
) -> LocalCompatibleProbe:
    runtime_clock = clock or _utc_now
    _require_probe_gate(authorized, environment)
    await _validate_paths(authorized)
    loaded = await load_provider_configuration(
        authorized.configuration_path
    )
    configured_route, model = _select_probe_route(authorized, loaded)
    policy, identity = await _load_local_contracts(authorized)
    route = authorize_local_compatible_route(
        loaded,
        configured_route,
        policy,
        identity,
    )
    total_timeout_seconds = (
        authorized.per_case_timeout_seconds
        * authorized.maximum_provider_calls
    )
    deadline_at = runtime_clock() + timedelta(
        seconds=total_timeout_seconds
    )
    broker: ConfiguredCredentialBroker | None = None
    credential: CredentialLease | None = None
    try:
        broker, credential = await acquire_local_adapter_credential(
            loaded=loaded,
            route=route,
            identity=identity,
            credential_environment_variable=(
                authorized.credential_environment_variable
            ),
            environment=environment,
            clock=runtime_clock,
            deadline_at=deadline_at,
            lease_ttl_seconds=total_timeout_seconds,
        )
        selected_connector = connector or LocalCompatibleHttpCoreConnector(
            SystemProviderAddressResolver(),
            clock=runtime_clock,
        )
        backend = LocalCompatibleProbeBackend(
            selected_connector,
            route,
            credential,
            clock=runtime_clock,
        )
        runner = BoundedLocalCompatibleProbeRunner(
            backend,
            clock=runtime_clock,
            per_case_timeout_seconds=(
                authorized.per_case_timeout_seconds
            ),
            evidence_ttl_seconds=authorized.evidence_ttl_seconds,
        )
        async with asyncio.timeout(total_timeout_seconds):
            result = await runner.probe(
                route,
                model_revision_sha256=model.model_revision_sha256,
                cancellation=asyncio.Event(),
                deadline_at=deadline_at,
            )
    finally:
        if broker is not None and credential is not None:
            await broker.release(credential)
    await write_private_smoke_output(
        authorized.result_path,
        result.model_dump_json().encode(),
    )
    return result


def _require_probe_gate(
    authorized: AuthorizedLocalProbe,
    environment: Mapping[bytes, bytes] | None,
) -> None:
    selected_environment = (
        os.environb if environment is None else environment
    )
    variable = authorized.gate_environment_variable.encode("ascii")
    if selected_environment.get(variable) != ADAPTER_SMOKE_GATE_VALUE:
        raise ValueError("local capability probe environment gate is closed")


async def _validate_paths(authorized: AuthorizedLocalProbe) -> None:
    for path in (
        authorized.configuration_path,
        authorized.route_policy_path,
        authorized.identity_path,
    ):
        await validate_private_smoke_input(path)
    await validate_provider_smoke_output_path(authorized.result_path)


async def _load_local_contracts(
    authorized: AuthorizedLocalProbe,
) -> tuple[
    LocalCompatibleRoutePolicy,
    LocalCompatibleIdentityReference,
]:
    contents = await asyncio.gather(
        read_private_smoke_input(
            authorized.route_policy_path,
            maximum_bytes=MAXIMUM_LOCAL_PROBE_INPUT_BYTES,
        ),
        read_private_smoke_input(
            authorized.identity_path,
            maximum_bytes=MAXIMUM_LOCAL_PROBE_INPUT_BYTES,
        ),
    )
    return (
        LocalCompatibleRoutePolicy.model_validate_json(contents[0]),
        LocalCompatibleIdentityReference.model_validate_json(contents[1]),
    )


def _select_probe_route(
    authorized: AuthorizedLocalProbe,
    loaded: LoadedProviderConfiguration,
) -> tuple[ProviderRouteConfiguration, ProviderModelCapabilities]:
    routes = tuple(
        route
        for route in loaded.configuration.routes
        if (
            route.enabled
            and route.provider == "local-compatible"
            and route.model == authorized.model
        )
    )
    if len(routes) != 1:
        raise ValueError("local probe requires one enabled route")
    route = routes[0]
    models = tuple(
        model
        for model in loaded.configuration.models
        if (
            model.provider == route.provider
            and model.model == route.model
            and model.model_revision_sha256
            == route.model_revision_sha256
        )
    )
    policies = tuple(
        policy
        for policy in loaded.configuration.data_policies
        if (
            policy.provider == route.provider
            and policy.policy_revision_sha256
            == route.policy_revision_sha256
        )
    )
    if (
        len(models) != 1
        or len(policies) != 1
        or models[0].max_output_tokens < LOCAL_PROBE_OUTPUT_TOKENS
        or DataClassification.PUBLIC
        not in policies[0].accepted_classifications
        or policies[0].retention_days != 0
        or policies[0].training_enabled
    ):
        raise ValueError("local probe route policy is ineligible")
    return route, models[0]


def _utc_now() -> datetime:
    return datetime.now(UTC)
