"""Disposable process tree used only by the cancellation containment drill."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def run_leaf() -> None:
    while True:
        signal.pause()


def run_parent(pid_path: Path) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen(
        (
            sys.executable,
            str(Path(__file__).resolve()),
            "--mode",
            "leaf",
        ),
        close_fds=True,
    )
    pid_path.write_text(
        json.dumps(
            {"child_pid": child.pid, "parent_pid": os.getpid()},
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    try:
        while child.poll() is None:
            time.sleep(0.01)
        while True:
            signal.pause()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("leaf", "parent"), required=True)
    parser.add_argument("--pid-path", type=Path)
    arguments = parser.parse_args()
    if arguments.mode == "leaf":
        run_leaf()
        return 0
    if arguments.pid_path is None:
        parser.error("--pid-path is required for parent mode")
    run_parent(arguments.pid_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
