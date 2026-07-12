from __future__ import annotations

import stat

from benchmarks.infrastructure import create_local_env


def test_private_env_writer_uses_owner_only_permissions(tmp_path, monkeypatch) -> None:
    environment_path = tmp_path / ".env.benchmark"
    monkeypatch.setattr(create_local_env, "ENV_PATH", environment_path)

    create_local_env.write_private_env("BENCHMARK_BIND_ADDRESS=127.0.0.1\n")

    assert environment_path.read_text(encoding="utf-8") == (
        "BENCHMARK_BIND_ADDRESS=127.0.0.1\n"
    )
    assert stat.S_IMODE(environment_path.stat().st_mode) == 0o600
