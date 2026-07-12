"""Fail early when the local benchmark host cannot safely run P1.6 controls."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

try:
    from .benchmark_common import load_env
except ImportError:  # Direct script execution keeps this benchmark self-contained.
    from benchmark_common import load_env

ROOT = Path(__file__).resolve().parent
COMPOSE_PATH = ROOT / "compose.yaml"
ENV_PATH = ROOT / ".env.benchmark"
MIN_MEMORY_BYTES = 8 * 1024**3
MIN_DISK_BYTES = 30 * 1024**3
MIN_MAX_MAP_COUNT = 262_144


def command(*args: str) -> str:
    return subprocess.check_output(
        args, text=True, stderr=subprocess.STDOUT, timeout=30
    ).strip()


def main() -> None:
    if not ENV_PATH.is_file():
        raise SystemExit("Create .env.benchmark with create_local_env.py first")
    load_env()
    info = json.loads(command("docker", "info", "--format", "{{json .}}"))
    if info["MemTotal"] < MIN_MEMORY_BYTES:
        raise SystemExit("Docker memory is below the 8 GiB benchmark minimum")
    if shutil.disk_usage(ROOT).free < MIN_DISK_BYTES:
        raise SystemExit("Free disk is below the 30 GiB benchmark minimum")
    max_map_count = int(Path("/proc/sys/vm/max_map_count").read_text().strip())
    if max_map_count < MIN_MAX_MAP_COUNT:
        raise SystemExit("vm.max_map_count is below the OpenSearch minimum")
    command(
        "docker",
        "compose",
        "--env-file",
        str(ENV_PATH),
        "-f",
        str(COMPOSE_PATH),
        "config",
        "--quiet",
    )
    print(
        "Preflight passed: Docker, memory, disk, vm.max_map_count, and Compose config"
    )


if __name__ == "__main__":
    main()
