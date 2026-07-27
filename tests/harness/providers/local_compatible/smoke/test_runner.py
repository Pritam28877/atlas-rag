import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path

import pytest

from app.cli.harness.local_adapter_credentials import (
    acquire_local_adapter_credential,
)
from app.cli.harness.local_adapter_smoke_runner import (
    run_local_adapter_smoke,
)
from app.cli.harness.provider_smoke_decode import EXPECTED_SMOKE_RESPONSE
from app.services.harness.providers.egress_contracts import ProviderEgressRequest
from app.services.harness.providers.local_compatible_identity import (
    LocalAuthenticationMode,
)
from app.services.harness.providers.local_compatible_policy import (
    authorize_local_compatible_route,
)
from app.services.harness.providers.local_compatible_stream_transport import (
    LocalCompatibleTransportError,
)
from tests.harness.providers.local_compatible.fixtures import (
    NOW,
    configuration,
    identity,
    route_policy,
)
from tests.harness.providers.local_compatible.smoke.fixtures import (
    authorized_smoke,
    completed_sse,
)


class RecordingConnector:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks
        self.calls = 0
        self.closed = False
        self.request: ProviderEgressRequest | None = None

    def stream(self, request, *args, **kwargs) -> AsyncGenerator[bytes, None]:
        del args, kwargs
        self.calls += 1
        self.request = request
        return self._stream()

    async def _stream(self) -> AsyncGenerator[bytes, None]:
        try:
            for chunk in self._chunks:
                yield chunk
        finally:
            self.closed = True


def test_local_smoke_runs_one_production_adapter_call_and_redacts(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    authorized = authorized_smoke(tmp_path)
    connector = RecordingConnector(completed_sse())

    result = asyncio.run(
        run_local_adapter_smoke(
            authorized,
            connector=connector,
            environment={b"ATLAS_LOCAL_SMOKE_ENABLED": b"enabled"},
            clock=lambda: NOW,
            monotonic_clock=lambda: 1.0,
        )
    )

    assert connector.calls == 1
    assert connector.closed
    assert connector.request is not None
    assert connector.request.metadata.provider == "local-compatible"
    assert result.canonical_shape == "text_usage_completed"
    assert result.cancellation_verified
    assert result.cancellation_latency_ms == 0
    assert result.latency_ms == 0
    assert result.charged_cost_microusd == 0
    assert result.active_credential_leases == 0
    persisted = authorized.result_path.read_text()
    database = authorized.database_path.read_bytes()
    assert EXPECTED_SMOKE_RESPONSE not in persisted
    assert EXPECTED_SMOKE_RESPONSE.encode() not in database
    assert authorized.result_path.stat().st_mode & 0o077 == 0
    assert _reservation_status(authorized.database_path) == "settled"


def test_closed_gate_has_no_files_or_provider_side_effect(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    authorized = authorized_smoke(tmp_path)
    connector = RecordingConnector(completed_sse())

    with pytest.raises(ValueError, match="gate is closed"):
        asyncio.run(
            run_local_adapter_smoke(
                authorized,
                connector=connector,
                environment={},
                clock=lambda: NOW,
            )
        )

    assert connector.calls == 0
    assert not authorized.database_path.exists()
    assert not authorized.result_path.exists()


def test_malformed_stream_releases_cost_and_writes_no_result(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    authorized = authorized_smoke(tmp_path)
    connector = RecordingConnector((b"data: {}\n\n",))

    with pytest.raises(LocalCompatibleTransportError):
        asyncio.run(
            run_local_adapter_smoke(
                authorized,
                connector=connector,
                environment={
                    b"ATLAS_LOCAL_SMOKE_ENABLED": b"enabled"
                },
                clock=lambda: NOW,
            )
        )

    assert connector.calls == 1
    assert connector.closed
    assert authorized.database_path.exists()
    assert not authorized.result_path.exists()
    assert _reservation_status(authorized.database_path) == "released"


def test_bearer_environment_must_match_identity_reference() -> None:
    loaded, configured_route = configuration()
    bearer_identity = identity(
        LocalAuthenticationMode.BEARER_ENVIRONMENT,
        bearer_environment_variable="LOCAL_MODEL_TOKEN",
    )
    route = authorize_local_compatible_route(
        loaded,
        configured_route,
        route_policy(),
        bearer_identity,
    )

    with pytest.raises(ValueError, match="bearer environment is missing"):
        asyncio.run(
            acquire_local_adapter_credential(
                loaded=loaded,
                route=route,
                identity=bearer_identity,
                credential_environment_variable="OTHER_MODEL_TOKEN",
                environment={b"OTHER_MODEL_TOKEN": b"secret"},
                clock=lambda: NOW,
                deadline_at=NOW + timedelta(seconds=10),
                lease_ttl_seconds=10,
            )
        )


def _reservation_status(database_path: Path) -> str:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT reservation_status "
            "FROM harness_provider_cost_reservations"
        ).fetchone()
    assert row is not None
    return str(row[0])
