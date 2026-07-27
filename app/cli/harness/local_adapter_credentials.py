"""Optional environment-bearer acquisition for authorized local routes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime

from app.services.harness.providers import (
    ConfiguredCredentialBroker,
    EnvironmentCredentialBackend,
    EnvironmentCredentialReference,
    LoadedProviderConfiguration,
)
from app.services.harness.providers.credential_material import CredentialLease
from app.services.harness.providers.local_compatible_identity import (
    LocalAuthenticationMode,
    LocalCompatibleIdentityReference,
)
from app.services.harness.providers.local_compatible_policy import (
    AuthorizedLocalCompatibleRoute,
)


async def acquire_local_adapter_credential(
    *,
    loaded: LoadedProviderConfiguration,
    route: AuthorizedLocalCompatibleRoute,
    identity: LocalCompatibleIdentityReference,
    credential_environment_variable: str | None,
    environment: Mapping[bytes, bytes] | None,
    clock: Callable[[], datetime],
    deadline_at: datetime,
    lease_ttl_seconds: int,
) -> tuple[
    ConfiguredCredentialBroker | None,
    CredentialLease | None,
]:
    if identity.authentication is LocalAuthenticationMode.NONE:
        if credential_environment_variable is not None:
            raise ValueError("credential environment is unexpected")
        return None, None
    if (
        credential_environment_variable is None
        or credential_environment_variable
        != identity.bearer_environment_variable
    ):
        raise ValueError("local bearer environment is missing")
    backend = EnvironmentCredentialBackend(
        (
            EnvironmentCredentialReference(
                handle=route.credential_handle,
                environment_variable=credential_environment_variable,
                lease_ttl_seconds=lease_ttl_seconds,
            ),
        ),
        development_mode=True,
        clock=clock,
        environment=environment,
    )
    broker = ConfiguredCredentialBroker(
        loaded,
        backend,
        clock=clock,
        maximum_active_leases=1,
    )
    credential = await broker.acquire(
        route.credential_handle,
        provider="local-compatible",
        destination_sha256=route.destination_sha256,
        cancellation=asyncio.Event(),
        deadline_at=deadline_at,
    )
    return broker, credential
