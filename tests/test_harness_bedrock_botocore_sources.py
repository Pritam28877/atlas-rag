import os
from pathlib import Path

import pytest

from app.services.harness.providers import (
    BedrockCredentialResolutionError,
    BedrockCredentialSourceKind,
    BedrockIdentityReference,
)
from app.services.harness.providers.bedrock_botocore_sources import (
    BotocoreStaticCredentialBackend,
)


def test_environment_source_reads_only_explicit_aws_fields() -> None:
    backend = BotocoreStaticCredentialBackend(
        environment={
            "AWS_ACCESS_KEY_ID": "environment-access",
            "AWS_SECRET_ACCESS_KEY": "environment-secret",
            "AWS_SESSION_TOKEN": "environment-token",
            "UNRELATED_SECRET": "must-not-be-read",
        }
    )

    material = backend.load(_identity(BedrockCredentialSourceKind.ENVIRONMENT))

    access_key, secret_key, token = material.views()
    assert bytes(access_key) == b"environment-access"
    assert bytes(secret_key) == b"environment-secret"
    assert token is not None and bytes(token) == b"environment-token"
    material.zero()


def test_profile_source_uses_only_private_explicit_file(
    tmp_path: Path,
) -> None:
    credentials_file = tmp_path / "credentials"
    credentials_file.write_text(
        "[atlas-test]\n"
        "aws_access_key_id=profile-access\n"
        "aws_secret_access_key=profile-secret\n"
        "aws_session_token=profile-token\n"
    )
    os.chmod(credentials_file, 0o600)
    backend = BotocoreStaticCredentialBackend(environment={})

    material = backend.load(
        _identity(
            BedrockCredentialSourceKind.PROFILE,
            profile_name="atlas-test",
            shared_credentials_file=credentials_file,
        )
    )

    access_key, secret_key, token = material.views()
    assert bytes(access_key) == b"profile-access"
    assert bytes(secret_key) == b"profile-secret"
    assert token is not None and bytes(token) == b"profile-token"
    material.zero()


def test_profile_source_rejects_public_file_and_missing_profile(
    tmp_path: Path,
) -> None:
    credentials_file = tmp_path / "credentials"
    credentials_file.write_text(
        "[other]\naws_access_key_id=access\naws_secret_access_key=secret\n"
    )
    os.chmod(credentials_file, 0o644)
    backend = BotocoreStaticCredentialBackend(environment={})
    reference = _identity(
        BedrockCredentialSourceKind.PROFILE,
        profile_name="atlas-test",
        shared_credentials_file=credentials_file,
    )

    with pytest.raises(BedrockCredentialResolutionError):
        backend.load(reference)
    os.chmod(credentials_file, 0o600)
    with pytest.raises(BedrockCredentialResolutionError):
        backend.load(reference)


def test_selected_static_backend_rejects_network_sources() -> None:
    backend = BotocoreStaticCredentialBackend(environment={})

    with pytest.raises(BedrockCredentialResolutionError):
        backend.load(_identity(BedrockCredentialSourceKind.INSTANCE_METADATA))


def _identity(
    source: BedrockCredentialSourceKind,
    **updates: object,
) -> BedrockIdentityReference:
    values = {
        "identity_reference_id": "awsid_" + "1" * 32,
        "credential_handle": "pcr_" + "2" * 32,
        "source": source,
        **updates,
    }
    return BedrockIdentityReference.model_validate(values)
