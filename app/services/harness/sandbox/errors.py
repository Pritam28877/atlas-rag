"""Stable sanitized sandbox lifecycle errors."""


class SandboxSupervisorError(RuntimeError):
    """Sanitized sandbox lifecycle failure."""


class SandboxOutputLimitExceeded(SandboxSupervisorError):
    """The child exceeded its combined stdout/stderr allowance."""


class SandboxProcessTimedOut(SandboxSupervisorError):
    """The child exceeded its wall-time allowance."""


class SandboxProcessCancelled(SandboxSupervisorError):
    """The caller cancelled the owned process tree."""


class SandboxResourceCleanupError(SandboxSupervisorError):
    """The owned process tree or resource scope did not cleanly close."""
