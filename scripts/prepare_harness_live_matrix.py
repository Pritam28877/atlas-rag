"""Prepare a private live-matrix manifest from conformance evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from app.cli.harness import prepare_live_matrix_manifest


def main(arguments: Sequence[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    try:
        manifest = asyncio.run(
            prepare_live_matrix_manifest(
                parsed.conformance_report_path,
                parsed.preparation_path,
                parsed.manifest_path,
            )
        )
    except Exception:
        _write_status({"status": "failed"}, sys.stderr)
        return 1
    _write_status(
        {
            "providers": len(manifest.providers),
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
        "--conformance-report-path",
        required=True,
        type=Path,
    )
    parser.add_argument(
        "--preparation-path",
        required=True,
        type=Path,
    )
    parser.add_argument(
        "--manifest-path",
        required=True,
        type=Path,
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
