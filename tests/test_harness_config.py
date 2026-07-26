from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import MEBIBYTE, Settings


def test_harness_is_disabled_and_bounded_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.harness.enabled is False
    assert settings.harness.workspace_root is None
    assert settings.harness.state_directory is None
    assert settings.harness.isolation_executable is None
    assert settings.harness.provider_config_path is None
    assert settings.harness.local_transport == "stdio"
    assert settings.harness.unix_socket_path is None
    assert settings.harness.loopback_http_enabled is False
    assert settings.harness.active_sessions == 8
    assert settings.harness.provider_concurrency == 4
    assert settings.harness.provider_queue_size == 128
    assert settings.harness.subscriber_queue_events == 256
    assert settings.harness.tool_output_max_bytes == MEBIBYTE
    assert settings.harness.cloud_spend_limit_usd == Decimal("0")
    assert settings.harness.tool_network_enabled is False
    assert settings.harness.telemetry_export_enabled is False


def test_disabled_harness_does_not_require_runtime_paths() -> None:
    settings = Settings(_env_file=None)

    assert settings.harness.enabled is False


def test_enabled_harness_requires_all_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS__ENABLED", "true")

    with pytest.raises(ValidationError, match="enabled harness requires settings"):
        Settings(_env_file=None)


def test_enabled_harness_accepts_absolute_distinct_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "HARNESS__ENABLED": "true",
        "HARNESS__WORKSPACE_ROOT": "/srv/atlas/workspaces",
        "HARNESS__STATE_DIRECTORY": "/var/lib/atlas",
        "HARNESS__ISOLATION_EXECUTABLE": "/usr/bin/bwrap",
        "HARNESS__PROVIDER_CONFIG_PATH": "/etc/atlas/providers.json",
        "HARNESS__ACTIVE_SESSIONS": "4",
        "HARNESS__CLOUD_SPEND_LIMIT_USD": "1.2500",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert settings.harness.enabled is True
    assert settings.harness.active_sessions == 4
    assert settings.harness.cloud_spend_limit_usd == Decimal("1.2500")
    assert settings.harness.provider_config_path == Path(
        "/etc/atlas/providers.json"
    )


def test_enabled_harness_requires_provider_config_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "HARNESS__ENABLED": "true",
        "HARNESS__WORKSPACE_ROOT": "/srv/atlas/workspaces",
        "HARNESS__STATE_DIRECTORY": "/var/lib/atlas",
        "HARNESS__ISOLATION_EXECUTABLE": "/usr/bin/bwrap",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match="provider_config_path"):
        Settings(_env_file=None)


def test_enabled_harness_rejects_relative_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "HARNESS__ENABLED": "true",
        "HARNESS__WORKSPACE_ROOT": "relative/workspaces",
        "HARNESS__STATE_DIRECTORY": "/var/lib/atlas",
        "HARNESS__ISOLATION_EXECUTABLE": "/usr/bin/bwrap",
        "HARNESS__PROVIDER_CONFIG_PATH": "/etc/atlas/providers.json",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match="paths must be absolute"):
        Settings(_env_file=None)

    monkeypatch.setenv(
        "HARNESS__WORKSPACE_ROOT",
        "/srv/atlas/workspaces",
    )
    monkeypatch.setenv(
        "HARNESS__PROVIDER_CONFIG_PATH",
        "relative/providers.json",
    )
    with pytest.raises(ValidationError, match="paths must be absolute"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("workspace_root", "state_directory"),
    [
        ("/srv/atlas", "/srv/atlas"),
        ("/srv/atlas", "/srv/atlas/state"),
        ("/srv/atlas/workspaces", "/srv/atlas"),
    ],
)
def test_enabled_harness_separates_workspace_and_state(
    monkeypatch: pytest.MonkeyPatch,
    workspace_root: str,
    state_directory: str,
) -> None:
    environment = {
        "HARNESS__ENABLED": "true",
        "HARNESS__WORKSPACE_ROOT": workspace_root,
        "HARNESS__STATE_DIRECTORY": state_directory,
        "HARNESS__ISOLATION_EXECUTABLE": "/usr/bin/bwrap",
        "HARNESS__PROVIDER_CONFIG_PATH": "/etc/atlas/providers.json",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match="must not overlap"):
        Settings(_env_file=None)


def test_enabled_harness_keeps_isolation_binary_outside_writable_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "HARNESS__ENABLED": "true",
        "HARNESS__WORKSPACE_ROOT": "/srv/atlas/workspaces",
        "HARNESS__STATE_DIRECTORY": "/var/lib/atlas",
        "HARNESS__ISOLATION_EXECUTABLE": "/srv/atlas/workspaces/bwrap",
        "HARNESS__PROVIDER_CONFIG_PATH": "/etc/atlas/providers.json",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match="outside writable roots"):
        Settings(_env_file=None)


def test_enabled_harness_keeps_provider_config_outside_writable_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "HARNESS__ENABLED": "true",
        "HARNESS__WORKSPACE_ROOT": "/srv/atlas/workspaces",
        "HARNESS__STATE_DIRECTORY": "/var/lib/atlas",
        "HARNESS__ISOLATION_EXECUTABLE": "/usr/bin/bwrap",
        "HARNESS__PROVIDER_CONFIG_PATH": (
            "/srv/atlas/workspaces/providers.json"
        ),
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match="outside writable roots"):
        Settings(_env_file=None)


def test_unix_transport_requires_state_contained_absolute_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_environment = {
        "HARNESS__ENABLED": "true",
        "HARNESS__WORKSPACE_ROOT": "/srv/atlas/workspaces",
        "HARNESS__STATE_DIRECTORY": "/var/lib/atlas",
        "HARNESS__ISOLATION_EXECUTABLE": "/usr/bin/bwrap",
        "HARNESS__PROVIDER_CONFIG_PATH": "/etc/atlas/providers.json",
        "HARNESS__LOCAL_TRANSPORT": "unix",
    }
    for name, value in base_environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match="requires unix_socket_path"):
        Settings(_env_file=None)

    monkeypatch.setenv("HARNESS__UNIX_SOCKET_PATH", "/tmp/atlas.sock")
    with pytest.raises(ValidationError, match="inside harness state_directory"):
        Settings(_env_file=None)

    monkeypatch.setenv(
        "HARNESS__UNIX_SOCKET_PATH",
        "/var/lib/atlas/run/atlas.sock",
    )
    settings = Settings(_env_file=None)
    assert settings.harness.local_transport == "unix"


def test_stdio_transport_rejects_ambiguous_unix_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "HARNESS__ENABLED": "true",
        "HARNESS__WORKSPACE_ROOT": "/srv/atlas/workspaces",
        "HARNESS__STATE_DIRECTORY": "/var/lib/atlas",
        "HARNESS__ISOLATION_EXECUTABLE": "/usr/bin/bwrap",
        "HARNESS__PROVIDER_CONFIG_PATH": "/etc/atlas/providers.json",
        "HARNESS__UNIX_SOCKET_PATH": "/var/lib/atlas/run/atlas.sock",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match="stdio harness"):
        Settings(_env_file=None)


def test_loopback_http_cannot_be_enabled_in_this_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS__LOOPBACK_HTTP_ENABLED", "true")

    with pytest.raises(ValidationError, match="False"):
        Settings(_env_file=None)


def test_harness_settings_contain_no_provider_credentials() -> None:
    settings = Settings(_env_file=None)
    secret_terms = {"api_key", "credential", "password", "secret", "token"}

    assert not any(
        term in field_name
        for field_name in type(settings.harness).model_fields
        for term in secret_terms
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("HARNESS__ACTIVE_SESSIONS", "65"),
        ("HARNESS__PROVIDER_CONCURRENCY", "17"),
        ("HARNESS__PROVIDER_QUEUE_SIZE", "513"),
        ("HARNESS__SUBSCRIBER_QUEUE_EVENTS", "2049"),
        ("HARNESS__REQUEST_MAX_BYTES", str(4 * MEBIBYTE + 1)),
        ("HARNESS__EVENT_PAYLOAD_MAX_BYTES", str(MEBIBYTE + 1)),
        ("HARNESS__TOOL_OUTPUT_MAX_BYTES", str(16 * MEBIBYTE + 1)),
        ("HARNESS__TURN_MAX_STEPS", "257"),
        ("HARNESS__TURN_MAX_SECONDS", "3601"),
        ("HARNESS__PROCESS_MAX_COUNT", "65"),
        ("HARNESS__REQUEST_TIMEOUT_SECONDS", "3601"),
        ("HARNESS__HANDSHAKE_TIMEOUT_SECONDS", "31"),
        ("HARNESS__SHUTDOWN_TIMEOUT_SECONDS", "31"),
        ("HARNESS__CLOUD_SPEND_LIMIT_USD", "10000.0001"),
    ],
)
def test_harness_rejects_values_above_hard_limits(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match="less than or equal to"):
        Settings(_env_file=None)
