from __future__ import annotations

import stat

import pytest

from benchmarks.infrastructure import create_local_env
from benchmarks.infrastructure.benchmark_common import require_loopback


def test_private_env_writer_uses_owner_only_permissions(tmp_path, monkeypatch) -> None:
    environment_path = tmp_path / ".env.benchmark"
    monkeypatch.setattr(create_local_env, "ENV_PATH", environment_path)

    create_local_env.write_private_env("BENCHMARK_BIND_ADDRESS=127.0.0.1\n")

    assert environment_path.read_text(encoding="utf-8") == (
        "BENCHMARK_BIND_ADDRESS=127.0.0.1\n"
    )
    assert stat.S_IMODE(environment_path.stat().st_mode) == 0o600


def test_non_loopback_benchmark_bind_address_is_rejected() -> None:
    with pytest.raises(ValueError, match="loopback"):
        require_loopback({"BENCHMARK_BIND_ADDRESS": "0.0.0.0"})
