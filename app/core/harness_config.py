from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from app.core.config_base import MEBIBYTE, ImmutableSettingsModel


class HarnessSettings(ImmutableSettingsModel):
    """Disabled-by-default, bounded configuration for Atlas Harness."""

    enabled: bool = False
    workspace_root: Path | None = None
    state_directory: Path | None = None
    isolation_executable: Path | None = None
    provider_config_path: Path | None = None
    local_transport: Literal["stdio", "unix"] = "stdio"
    unix_socket_path: Path | None = None
    loopback_http_enabled: Literal[False] = False
    active_sessions: int = Field(default=8, ge=1, le=64)
    queued_sessions: int = Field(default=64, ge=0, le=512)
    provider_concurrency: int = Field(default=4, ge=1, le=16)
    provider_queue_size: int = Field(default=128, ge=0, le=512)
    subscriber_queue_events: int = Field(default=256, ge=1, le=2048)
    request_max_bytes: int = Field(
        default=MEBIBYTE,
        ge=1024,
        le=4 * MEBIBYTE,
    )
    event_payload_max_bytes: int = Field(
        default=256 * 1024,
        ge=1024,
        le=MEBIBYTE,
    )
    tool_output_max_bytes: int = Field(
        default=MEBIBYTE,
        ge=1024,
        le=16 * MEBIBYTE,
    )
    turn_max_steps: int = Field(default=64, ge=1, le=256)
    turn_max_seconds: int = Field(default=600, ge=1, le=3600)
    process_max_count: int = Field(default=16, ge=1, le=64)
    request_timeout_seconds: float = Field(default=30, ge=0.001, le=3600)
    handshake_timeout_seconds: float = Field(default=5, ge=0.001, le=30)
    shutdown_timeout_seconds: float = Field(default=5, ge=0.001, le=30)
    cloud_spend_limit_usd: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        le=Decimal("10000"),
        max_digits=9,
        decimal_places=4,
    )
    tool_network_enabled: bool = False
    telemetry_export_enabled: bool = False

    @model_validator(mode="after")
    def validate_activation(self) -> Self:
        if not self.enabled:
            return self

        required_paths = {
            "workspace_root": self.workspace_root,
            "state_directory": self.state_directory,
            "isolation_executable": self.isolation_executable,
            "provider_config_path": self.provider_config_path,
        }
        missing_paths = [
            name for name, path in required_paths.items() if path is None
        ]
        if missing_paths:
            names = ", ".join(missing_paths)
            raise ValueError(f"enabled harness requires settings: {names}")

        relative_paths = [
            name
            for name, path in required_paths.items()
            if path is not None and not path.is_absolute()
        ]
        if relative_paths:
            names = ", ".join(relative_paths)
            raise ValueError(f"enabled harness paths must be absolute: {names}")
        if (
            self.workspace_root is None
            or self.state_directory is None
            or self.isolation_executable is None
            or self.provider_config_path is None
        ):
            raise ValueError("enabled harness path validation failed")

        workspace_root = self.workspace_root.resolve(strict=False)
        state_directory = self.state_directory.resolve(strict=False)
        isolation_executable = self.isolation_executable.resolve(strict=False)
        provider_config_path = self.provider_config_path.resolve(strict=False)
        if (
            workspace_root == state_directory
            or workspace_root.is_relative_to(state_directory)
            or state_directory.is_relative_to(workspace_root)
        ):
            raise ValueError(
                "harness workspace_root and state_directory must not overlap"
            )
        if isolation_executable.is_relative_to(workspace_root) or (
            isolation_executable.is_relative_to(state_directory)
        ):
            raise ValueError(
                "harness isolation_executable must be outside writable roots"
            )
        if provider_config_path.is_relative_to(workspace_root) or (
            provider_config_path.is_relative_to(state_directory)
        ):
            raise ValueError(
                "harness provider_config_path must be outside writable roots"
            )
        self._validate_transport(state_directory)
        return self

    def _validate_transport(self, state_directory: Path) -> None:
        if self.local_transport == "stdio":
            if self.unix_socket_path is not None:
                raise ValueError(
                    "stdio harness cannot configure a Unix socket path"
                )
            return
        if self.unix_socket_path is None:
            raise ValueError("Unix harness requires unix_socket_path")
        if not self.unix_socket_path.is_absolute():
            raise ValueError("Unix socket path must be absolute")
        socket_path = self.unix_socket_path.resolve(strict=False)
        if not socket_path.parent.is_relative_to(state_directory):
            raise ValueError(
                "Unix socket path must be inside harness state_directory"
            )
