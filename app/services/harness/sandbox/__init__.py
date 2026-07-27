"""Owned Atlas Harness OS-process isolation adapters."""

from app.services.harness.sandbox.admission import compile_sandbox_admission
from app.services.harness.sandbox.capabilities import (
    detect_platform_isolation,
    unsupported_platform_capabilities,
)
from app.services.harness.sandbox.environment import SandboxChildEnvironment
from app.services.harness.sandbox.errors import (
    SandboxOutputLimitExceeded,
    SandboxProcessCancelled,
    SandboxProcessTimedOut,
    SandboxSupervisorError,
)
from app.services.harness.sandbox.models import (
    SandboxCapabilityGrant,
    SandboxMount,
    SandboxMountAccess,
    SandboxNetworkMode,
    SandboxNetworkRule,
    SandboxProfile,
    SandboxResourceLimits,
)
from app.services.harness.sandbox.profile import (
    SandboxProfileError,
    compile_sandbox_profile,
    sandbox_destination_sha256,
)
from app.services.harness.sandbox.supervisor import (
    SandboxProcessResult,
    SandboxSupervisor,
)
from app.services.harness.sandbox.worker_scope_contracts import (
    WorkerScopeError,
    WorkerScopeRecord,
    WorkerScopeRequest,
    WorkerScopeState,
    WorkerWorkspaceView,
    compile_worker_scope,
    worker_scope_sha256,
    worker_workspace_view_sha256,
)
from app.services.harness.sandbox.worker_scope_manager import (
    InMemoryWorkerWorkspaceOwner,
    WorkerScopeManager,
)

__all__ = (
    "SandboxCapabilityGrant",
    "SandboxChildEnvironment",
    "SandboxMount",
    "SandboxMountAccess",
    "SandboxNetworkMode",
    "SandboxNetworkRule",
    "SandboxOutputLimitExceeded",
    "SandboxProcessCancelled",
    "SandboxProcessResult",
    "SandboxProcessTimedOut",
    "SandboxProfile",
    "SandboxProfileError",
    "SandboxResourceLimits",
    "SandboxSupervisor",
    "SandboxSupervisorError",
    "WorkerScopeError",
    "WorkerScopeRecord",
    "WorkerScopeRequest",
    "WorkerScopeState",
    "WorkerWorkspaceView",
    "InMemoryWorkerWorkspaceOwner",
    "WorkerScopeManager",
    "compile_worker_scope",
    "worker_scope_sha256",
    "worker_workspace_view_sha256",
    "compile_sandbox_profile",
    "compile_sandbox_admission",
    "detect_platform_isolation",
    "sandbox_destination_sha256",
    "unsupported_platform_capabilities",
)
