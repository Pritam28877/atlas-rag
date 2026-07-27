import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path

import pytest

from app.cli.harness.provider_smoke_decode import EXPECTED_SMOKE_RESPONSE
from app.cli.harness.vertex_adapter_smoke_runner import (
    run_vertex_adapter_smoke,
)
from app.services.harness.providers.credential_material import CredentialLease
from app.services.harness.providers.egress_contracts import ProviderEgressRequest
from app.services.harness.providers.vertex_credentials import (
    RefreshedVertexCredential,
)
from app.services.harness.providers.vertex_stream_transport import (
    VertexTransportError,
)
from tests.harness.providers.vertex.smoke.fixtures import (
    PRINCIPAL,
    authorized_smoke,
    completed_sse,
)
from tests.harness_provider_capability_fixtures import NOW


class RecordingConnector:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks
        self.calls = 0
        self.closed = False
        self.request: ProviderEgressRequest | None = None
        self.credential: CredentialLease | None = None

    def stream(
        self,
        request,
        route,
        credential,
        **kwargs,
    ) -> AsyncGenerator[bytes, None]:
        del route, kwargs
        self.calls += 1
        self.request = request
        self.credential = credential
        assert credential.secret_view() == b"vertex-smoke-token"
        return self._stream()

    async def _stream(self) -> AsyncGenerator[bytes, None]:
        try:
            for chunk in self._chunks:
                yield chunk
        finally:
            self.closed = True


class RecordingLoader:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, identity):
        del identity
        self.calls += 1
        return RefreshedVertexCredential(
            access_token="vertex-smoke-token",
            expires_at=NOW + timedelta(minutes=10),
            principal=PRINCIPAL,
            quota_project_id="atlas-test-12345",
        )


def test_vertex_smoke_runs_one_regional_call_and_redacts(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    authorized = authorized_smoke(tmp_path)
    connector = RecordingConnector(completed_sse())
    loader = RecordingLoader()

    result = asyncio.run(
        run_vertex_adapter_smoke(
            authorized,
            connector=connector,
            credential_loader=loader,
            environment={b"ATLAS_VERTEX_SMOKE_ENABLED": b"enabled"},
            clock=lambda: NOW,
            monotonic_clock=lambda: 1.0,
        )
    )

    assert loader.calls == 1
    assert connector.calls == 1
    assert connector.closed
    assert connector.credential is not None
    assert connector.credential.released
    assert connector.request is not None
    assert connector.request.metadata.provider == "vertex"
    assert result.canonical_shape == "text_usage_completed"
    assert result.cancellation_verified
    assert result.cancellation_latency_ms == 0
    assert result.latency_ms == 0
    assert result.charged_cost_microusd == 10_000
    persisted = authorized.result_path.read_text()
    database = authorized.database_path.read_bytes()
    assert EXPECTED_SMOKE_RESPONSE not in persisted
    assert EXPECTED_SMOKE_RESPONSE.encode() not in database
    assert _reservation_status(authorized.database_path) == "settled"


def test_closed_gate_prevents_adc_database_and_provider_side_effects(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    authorized = authorized_smoke(tmp_path)
    connector = RecordingConnector(completed_sse())
    loader = RecordingLoader()

    with pytest.raises(ValueError, match="gate is closed"):
        asyncio.run(
            run_vertex_adapter_smoke(
                authorized,
                connector=connector,
                credential_loader=loader,
                environment={},
                clock=lambda: NOW,
            )
        )

    assert loader.calls == 0
    assert connector.calls == 0
    assert not authorized.database_path.exists()


def test_malformed_attempt_settles_cap_releases_lease_and_writes_no_result(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    authorized = authorized_smoke(tmp_path)
    connector = RecordingConnector((b"data: {}\n\n",))

    with pytest.raises(VertexTransportError):
        asyncio.run(
            run_vertex_adapter_smoke(
                authorized,
                connector=connector,
                credential_loader=RecordingLoader(),
                environment={
                    b"ATLAS_VERTEX_SMOKE_ENABLED": b"enabled"
                },
                clock=lambda: NOW,
            )
        )

    assert connector.credential is not None
    assert connector.credential.released
    assert _reservation_status(authorized.database_path) == "settled"
    assert not authorized.result_path.exists()


def _reservation_status(database_path: Path) -> str:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT reservation_status "
            "FROM harness_provider_cost_reservations"
        ).fetchone()
    assert row is not None
    return str(row[0])
