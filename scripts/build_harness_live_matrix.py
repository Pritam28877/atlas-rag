"""Build a private live matrix from completed private smoke results."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from app.cli.harness import build_live_matrix_from_manifest


def main(arguments: Sequence[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    try:
        matrix = asyncio.run(
            build_live_matrix_from_manifest(parsed.manifest_path)
        )
    except Exception:
        _write_status({"status": "failed"}, sys.stderr)
        return 1
    _write_status(
        {
            "release_ready": matrix.release_ready,
            "status": "completed",
        },
        sys.stdout,
    )
    return 0


def _write_status(values: dict[str, object], stream) -> None:
    print(
        json.dumps(values, separators=(",", ":"), sort_keys=True),
        file=stream,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest-path",
        required=True,
        type=Path,
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
