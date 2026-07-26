import asyncio
import hashlib
import json
import os
import threading
from datetime import timedelta
from pathlib import Path

import pytest

import app.services.harness.providers.vertex_credentials as vertex_credentials
from app.services.harness.providers.credential_material import (
    CredentialBrokerError,
    CredentialBrokerErrorCode,
)
from app.services.harness.providers.vertex_credentials import (
    RefreshedVertexCredential,
    VertexCredentialBackend,
)
from app.services.harness.providers.vertex_identity import (
    VertexCredentialSourceKind,
)
from tests.harness.providers.vertex.fixtures import HANDLE, identity
from tests.harness_provider_capability_fixtures import NOW

PRINCIPAL = "runtime@atlas-test-12345.iam.gserviceaccount.com"
PRINCIPAL_SHA256 = hashlib.sha256(PRINCIPAL.encode()).hexdigest()


def _refreshed(
    *,
    access_token: str = "vertex-access-token",
    expires_at=NOW + timedelta(minutes=10),
    principal: str = PRINCIPAL,
    quota_project_id: str = "atlas-test-12345",
) -> RefreshedVertexCredential:
    return RefreshedVertexCredential(
        access_token=access_token,
        expires_at=expires_at,
        principal=principal,
        quota_project_id=quota_project_id,
    )


class RecordingLoader:
    def __init__(
        self,
        refreshed: RefreshedVertexCredential | None = None,
    ) -> None:
        self.refreshed = refreshed or _refreshed()
        self.thread_ids: list[int] = []

    def __call__(self, selected_identity):
        del selected_identity
        self.thread_ids.append(threading.get_ident())
        return self.refreshed


class FakeGoogleCredential:
    def __init__(self) -> None:
        self.token = "google-access-token"
        self.expiry = NOW + timedelta(minutes=10)
        self.quota_project_id = "atlas-test-12345"
        self.service_account_email = PRINCIPAL
        self.refreshed = False

    def refresh(self, request) -> None:
        del request
        self.refreshed = True


def test_refresh_runs_off_loop_and_returns_mutable_expiring_material() -> None:
    loader = RecordingLoader()
    backend = VertexCredentialBackend(
        (identity(expected_principal_sha256=PRINCIPAL_SHA256),),
        clock=lambda: NOW,
        loader=loader,
    )
    caller_thread = threading.get_ident()

    material = asyncio.run(backend.load(HANDLE))

    secret = material.claim()
    assert secret == bytearray(b"vertex-access-token")
    assert material.expires_at == NOW + timedelta(minutes=10)
    assert loader.thread_ids[0] != caller_thread
    secret[:] = bytes(len(secret))


@pytest.mark.parametrize(
    "refreshed",
    (
        _refreshed(principal="substituted@example.com"),
        _refreshed(quota_project_id="other-project-12345"),
        _refreshed(expires_at=NOW + timedelta(seconds=5)),
        _refreshed(access_token="not-ascii-\N{SNOWMAN}"),
    ),
)
def test_principal_quota_expiry_and_token_are_bound(
    refreshed: RefreshedVertexCredential,
) -> None:
    backend = VertexCredentialBackend(
        (identity(expected_principal_sha256=PRINCIPAL_SHA256),),
        clock=lambda: NOW,
        loader=RecordingLoader(refreshed),
    )

    with pytest.raises(CredentialBrokerError) as captured:
        asyncio.run(backend.load(HANDLE))

    assert captured.value.code is CredentialBrokerErrorCode.BACKEND
    assert "access_token" not in repr(captured.value)


def test_unknown_handle_and_duplicate_identity_fail_closed() -> None:
    backend = VertexCredentialBackend(
        (identity(expected_principal_sha256=PRINCIPAL_SHA256),),
        clock=lambda: NOW,
        loader=RecordingLoader(),
    )
    with pytest.raises(CredentialBrokerError) as unknown:
        asyncio.run(backend.load("pcr_" + "9" * 32))
    assert unknown.value.code is CredentialBrokerErrorCode.UNKNOWN_HANDLE

    with pytest.raises(ValueError, match="identities"):
        VertexCredentialBackend(
            (
                identity(expected_principal_sha256=PRINCIPAL_SHA256),
                identity(expected_principal_sha256=PRINCIPAL_SHA256),
            ),
            clock=lambda: NOW,
        )


def test_hash_bound_external_account_config_is_loaded_from_memory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    content = json.dumps(
        {"audience": "approved-pool", "type": "external_account"},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    path = tmp_path / "external-account.json"
    path.write_bytes(content)
    os.chmod(path, 0o600)
    captured: list[dict[str, object]] = []
    credential = FakeGoogleCredential()

    def load_from_dict(configuration, **kwargs):
        del kwargs
        captured.append(configuration)
        return credential, "atlas-test-12345"

    monkeypatch.setattr(
        vertex_credentials.google.auth,
        "load_credentials_from_dict",
        load_from_dict,
    )
    external_identity = identity(
        VertexCredentialSourceKind.EXTERNAL_ACCOUNT_FILE,
        expected_principal_sha256=PRINCIPAL_SHA256,
        external_account_file=path,
        external_account_file_sha256=hashlib.sha256(content).hexdigest(),
    )
    backend = VertexCredentialBackend(
        (external_identity,),
        clock=lambda: NOW,
    )

    material = asyncio.run(backend.load(HANDLE))

    assert credential.refreshed
    assert captured == [
        {"audience": "approved-pool", "type": "external_account"}
    ]
    material.zero()

    path.write_bytes(content + b"\n")
    os.chmod(path, 0o600)
    with pytest.raises(CredentialBrokerError) as changed:
        asyncio.run(backend.load(HANDLE))
    assert changed.value.code is CredentialBrokerErrorCode.BACKEND
