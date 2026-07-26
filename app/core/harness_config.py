from decimal import Decimal
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from app.core.config_base import MEBIBYTE, ImmutableSettingsModel


class HarnessSettings(ImmutableSettingsModel):
    """Disabled-by-default, bounded configuration for Atlas Harness."""

    enabled: bool = False
    workspace_root: Path | None = None
    state_directory: Path | None = None
    isolation_executable: Path | None = None
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
        ):
            raise ValueError("enabled harness path validation failed")

        workspace_root = self.workspace_root.resolve(strict=False)
        state_directory = self.state_directory.resolve(strict=False)
        isolation_executable = self.isolation_executable.resolve(strict=False)
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
        return self
