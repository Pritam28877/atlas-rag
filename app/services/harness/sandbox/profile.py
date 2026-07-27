"""Compile authorized capabilities into a bounded sandbox profile."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from app.services.harness.policy import (
    ExecutableTarget,
    FilesystemOperation,
    FilesystemTarget,
    NetworkTarget,
    ResourceTarget,
    SecretTarget,
)
from app.services.harness.protocol import OperationId
from app.services.harness.protocol.secret_delivery import SecretInjectionRequest
from app.services.harness.sandbox.models import (
    SandboxCapabilityGrant,
    SandboxMount,
    SandboxMountAccess,
    SandboxNetworkMode,
    SandboxNetworkRule,
    SandboxProfile,
    SandboxResourceLimits,
    _canonical_sha256,
)

MAXIMUM_SANDBOX_GRANTS = 64
SAFE_PARENT_ENVIRONMENT_NAMES = frozenset({"LANG", "LC_ALL", "TERM", "TZ"})
WORKSPACE_ROOT_URI = "file:///workspace"


class SandboxProfileError(ValueError):
    """An authorized capability set cannot produce a safe sandbox profile."""


def compile_sandbox_profile(
    *,
    operation_id: OperationId,
    workspace: Path,
    grants: tuple[SandboxCapabilityGrant, ...],
    default_limits: SandboxResourceLimits,
    secret_request: SecretInjectionRequest | None = None,
) -> SandboxProfile:
    """Compile exact grants without adding mounts, egress, secrets, or resources."""
    if not 1 <= len(grants) <= MAXIMUM_SANDBOX_GRANTS:
        raise SandboxProfileError("sandbox grants must contain 1 to 64 entries")
    workspace_root = _resolve_directory(workspace, "workspace")
    targets = tuple(grant.proposal.target for grant in grants)
    executable_targets = tuple(
        target for target in targets if isinstance(target, ExecutableTarget)
    )
    if len(executable_targets) != 1:
        raise SandboxProfileError("sandbox requires exactly one executable grant")
    executable_target = executable_targets[0]
    executable_source = _resolve_file(
        Path(executable_target.executable),
        "executable",
    )
    executable_destination = executable_target.executable
    working_directory = _sandbox_workspace_path(
        executable_target.working_directory
    )

    filesystem_targets = tuple(
        target for target in targets if isinstance(target, FilesystemTarget)
    )
    mounts = _compile_mounts(workspace_root, filesystem_targets)
    _require_working_directory(working_directory, mounts)

    network_targets = tuple(
        target for target in targets if isinstance(target, NetworkTarget)
    )
    network_rules = _compile_network_rules(network_targets)
    network_mode = (
        SandboxNetworkMode.PROXY
        if network_rules
        else SandboxNetworkMode.ISOLATED
    )
    limits = _compile_limits(grants, targets, default_limits)
    destination_sha256 = sandbox_destination_sha256(
        operation_id=operation_id,
        executable=executable_target,
    )
    inherited_names, secret_names = _compile_environment(
        operation_id=operation_id,
        destination_sha256=destination_sha256,
        targets=targets,
        secret_request=secret_request,
    )
    draft = SandboxProfile.model_construct(
        operation_id=operation_id,
        executable_source=executable_source,
        executable=executable_destination,
        arguments=executable_target.arguments,
        working_directory=working_directory,
        mounts=mounts,
        network_mode=network_mode,
        network_rules=network_rules,
        inherited_environment_names=inherited_names,
        secret_environment_names=secret_names,
        limits=limits,
        destination_sha256=destination_sha256,
        profile_sha256="0" * 64,
    )
    return SandboxProfile(
        operation_id=operation_id,
        executable_source=executable_source,
        executable=executable_destination,
        arguments=executable_target.arguments,
        working_directory=working_directory,
        mounts=mounts,
        network_mode=network_mode,
        network_rules=network_rules,
        inherited_environment_names=inherited_names,
        secret_environment_names=secret_names,
        limits=limits,
        destination_sha256=destination_sha256,
        profile_sha256=_canonical_sha256(
            draft.model_dump(mode="json", exclude={"profile_sha256"})
        ),
    )


def sandbox_destination_sha256(
    *,
    operation_id: OperationId,
    executable: ExecutableTarget,
) -> str:
    return _canonical_sha256(
        {
            "operation_id": operation_id,
            "executable": executable.model_dump(mode="json"),
        }
    )


def _compile_mounts(
    workspace: Path,
    targets: tuple[FilesystemTarget, ...],
) -> tuple[SandboxMount, ...]:
    mounts: list[SandboxMount] = []
    for target in targets:
        if target.root_uri != WORKSPACE_ROOT_URI:
            raise SandboxProfileError("only the selected workspace may be mounted")
        source = (workspace / target.relative_path).resolve(strict=True)
        if not source.is_relative_to(workspace):
            raise SandboxProfileError("sandbox mount escapes the workspace")
        access = (
            SandboxMountAccess.READ_WRITE
            if target.operation is FilesystemOperation.WRITE
            else SandboxMountAccess.READ_ONLY
        )
        mounts.append(
            SandboxMount(
                source=source,
                destination=_sandbox_workspace_path(target.relative_path),
                access=access,
            )
        )
    mounts.sort(key=lambda mount: (mount.destination, mount.access.value))
    destinations = tuple(mount.destination for mount in mounts)
    if len(destinations) != len(set(destinations)):
        raise SandboxProfileError("sandbox mount destinations must be unique")
    for index, destination in enumerate(destinations):
        path = PurePosixPath(destination)
        if any(
            path.is_relative_to(PurePosixPath(other))
            or PurePosixPath(other).is_relative_to(path)
            for other in destinations[index + 1 :]
        ):
            raise SandboxProfileError("overlapping sandbox mounts are forbidden")
    return tuple(mounts)


def _compile_network_rules(
    targets: tuple[NetworkTarget, ...],
) -> tuple[SandboxNetworkRule, ...]:
    rules = tuple(
        sorted(
            (
                SandboxNetworkRule(
                    scheme=target.scheme,
                    host=target.host,
                    port=target.port,
                    method=target.method.value,
                    follow_redirects=target.follow_redirects,
                )
                for target in targets
            ),
            key=lambda rule: (
                rule.scheme,
                rule.host,
                rule.port,
                rule.method,
                rule.follow_redirects,
            ),
        )
    )
    if len(rules) != len(set(rule.model_dump_json() for rule in rules)):
        raise SandboxProfileError("sandbox network rules must be unique")
    return rules


def _compile_limits(
    grants: tuple[SandboxCapabilityGrant, ...],
    targets: tuple[object, ...],
    defaults: SandboxResourceLimits,
) -> SandboxResourceLimits:
    policy_limits = tuple(grant.decision.effective_limits for grant in grants)
    resource_targets = tuple(
        target for target in targets if isinstance(target, ResourceTarget)
    )
    if len(resource_targets) > 1:
        raise SandboxProfileError("sandbox accepts at most one resource grant")
    resource = resource_targets[0] if resource_targets else None
    return SandboxResourceLimits(
        wall_time_ms=_minimum_limit(
            defaults.wall_time_ms,
            tuple(limit.max_duration_ms for limit in policy_limits),
            resource.max_wall_time_ms if resource else None,
        ),
        cpu_time_ms=_minimum_limit(
            defaults.cpu_time_ms,
            tuple(limit.max_cpu_ms for limit in policy_limits),
            resource.max_cpu_ms if resource else None,
        ),
        memory_bytes=_minimum_limit(
            defaults.memory_bytes,
            tuple(limit.max_memory_bytes for limit in policy_limits),
            resource.max_memory_bytes if resource else None,
        ),
        output_bytes=_minimum_limit(
            defaults.output_bytes,
            tuple(limit.max_output_bytes for limit in policy_limits),
            resource.max_output_bytes if resource else None,
        ),
        processes=_minimum_limit(
            defaults.processes,
            tuple(limit.max_processes for limit in policy_limits),
            resource.max_processes if resource else None,
        ),
    )


def _compile_environment(
    *,
    operation_id: OperationId,
    destination_sha256: str,
    targets: tuple[object, ...],
    secret_request: SecretInjectionRequest | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if secret_request is None:
        return (), ()
    if (
        secret_request.operation_id != operation_id
        or secret_request.destination_sha256 != destination_sha256
    ):
        raise SandboxProfileError("secret request destination does not match")
    inherited_names = secret_request.inherited_environment_names
    if not set(inherited_names).issubset(SAFE_PARENT_ENVIRONMENT_NAMES):
        raise SandboxProfileError("parent environment request is not safe")
    authorized_secrets = {
        (target.secret_handle, target.destination_sha256)
        for target in targets
        if isinstance(target, SecretTarget)
    }
    requested_secrets = {
        (binding.secret_handle, destination_sha256)
        for binding in secret_request.bindings
    }
    if not requested_secrets.issubset(authorized_secrets):
        raise SandboxProfileError("secret request is not capability-authorized")
    return inherited_names, tuple(
        sorted(binding.environment_name for binding in secret_request.bindings)
    )


def _minimum_limit(
    default: int,
    policy_values: tuple[int | None, ...],
    resource_value: int | None,
) -> int:
    values = [default, *(value for value in policy_values if value is not None)]
    if resource_value is not None:
        values.append(resource_value)
    return min(values)


def _sandbox_workspace_path(relative_path: str) -> str:
    if relative_path in {"", "."}:
        return "/workspace"
    path = PurePosixPath("/workspace") / relative_path
    if ".." in path.parts:
        raise SandboxProfileError("sandbox path traversal is forbidden")
    return path.as_posix()


def _require_working_directory(
    working_directory: str,
    mounts: tuple[SandboxMount, ...],
) -> None:
    if working_directory == "/workspace":
        return
    working_path = PurePosixPath(working_directory)
    if not any(
        working_path == PurePosixPath(mount.destination)
        or working_path.is_relative_to(PurePosixPath(mount.destination))
        for mount in mounts
        if mount.source.is_dir()
    ):
        raise SandboxProfileError("working directory is not mounted")


def _resolve_directory(path: Path, field: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise SandboxProfileError(f"{field} is unavailable") from error
    if not resolved.is_dir():
        raise SandboxProfileError(f"{field} must be a directory")
    return resolved


def _resolve_file(path: Path, field: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise SandboxProfileError(f"{field} is unavailable") from error
    if not resolved.is_file():
        raise SandboxProfileError(f"{field} must be a file")
    return resolved
