"""Bounded redacted child environment tests."""

from pathlib import Path

import pytest

from app.services.harness.policy import (
    FilesystemOperation,
    FilesystemTarget,
    SecretTarget,
)
from app.services.harness.protocol.secret_delivery import (
    ChildSecretBinding,
    SecretDeliveryReceipt,
    SecretInjectionRequest,
    secret_delivery_receipt_sha256,
)
from app.services.harness.sandbox import (
    SandboxChildEnvironment,
    compile_sandbox_profile,
    sandbox_destination_sha256,
)
from app.services.harness.sandbox.environment import (
    SandboxEnvironmentError,
    prepare_child_environment,
)
from tests.harness.policy.fixtures import NOW
from tests.harness.sandbox.fixtures import (
    OPERATION_ID,
    defaults,
    executable_target,
    grant,
)


def secret_profile(tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir()
    executable = executable_target()
    destination = sandbox_destination_sha256(
        operation_id=OPERATION_ID,
        executable=executable,
    )
    request = SecretInjectionRequest(
        operation_id=OPERATION_ID,
        destination_sha256=destination,
        bindings=(
            ChildSecretBinding(
                secret_handle="secret:provider/api-key",
                environment_name="PROVIDER_API_KEY",
            ),
        ),
        inherited_environment_names=(),
    )
    profile = compile_sandbox_profile(
        operation_id=OPERATION_ID,
        workspace=tmp_path,
        grants=(
            grant(executable, "executable.run"),
            grant(
                FilesystemTarget(
                    root_uri="file:///workspace",
                    relative_path="work",
                    operation=FilesystemOperation.READ,
                ),
                "filesystem.read",
            ),
            grant(
                SecretTarget(
                    secret_handle="secret:provider/api-key",
                    destination_sha256=destination,
                ),
                "secret.read",
            ),
        ),
        default_limits=defaults(),
        secret_request=request,
    )
    receipt_values = {
        "operation_id": OPERATION_ID,
        "destination_sha256": destination,
        "secret_handle_sha256s": ("a" * 64,),
        "environment_names": ("PROVIDER_API_KEY",),
        "delivered_at": NOW,
    }
    receipt = SecretDeliveryReceipt(
        **receipt_values,
        receipt_sha256=secret_delivery_receipt_sha256(**receipt_values),
    )
    return profile, receipt


def test_readonly_secret_is_packetized_without_repr_disclosure(
    tmp_path: Path,
) -> None:
    profile, receipt = secret_profile(tmp_path)
    secret = bytearray(b"synthetic-secret")
    environment = SandboxChildEnvironment(
        operation_id=profile.operation_id,
        destination_sha256=profile.destination_sha256,
        parent_environment={},
        secret_environment={
            "PROVIDER_API_KEY": memoryview(secret).toreadonly(),
        },
        receipt=receipt,
    )

    _, packet = prepare_child_environment(profile, environment)

    assert b"synthetic-secret" in packet
    assert "synthetic-secret" not in repr(environment)
    packet[:] = bytes(len(packet))
    assert b"synthetic-secret" not in packet


def test_mutable_or_oversized_environment_fails_closed(tmp_path: Path) -> None:
    profile, receipt = secret_profile(tmp_path)
    mutable = SandboxChildEnvironment(
        operation_id=profile.operation_id,
        destination_sha256=profile.destination_sha256,
        parent_environment={},
        secret_environment={
            "PROVIDER_API_KEY": memoryview(bytearray(b"mutable")),
        },
        receipt=receipt,
    )
    with pytest.raises(SandboxEnvironmentError, match="invalid"):
        prepare_child_environment(profile, mutable)

    with pytest.raises(SandboxEnvironmentError, match="entry limit"):
        SandboxChildEnvironment(
            operation_id=profile.operation_id,
            destination_sha256=profile.destination_sha256,
            parent_environment={f"NAME_{index}": "x" for index in range(65)},
            secret_environment={},
            receipt=None,
        )
