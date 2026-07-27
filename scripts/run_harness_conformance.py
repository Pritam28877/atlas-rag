"""Run the packaged offline provider conformance suite."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from app.cli.harness import (
    PackagedConformanceReportRequest,
    run_packaged_conformance_report,
)


def main(arguments: Sequence[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    try:
        request = PackagedConformanceReportRequest(
            timeout_seconds=parsed.timeout_seconds,
            result_path=parsed.result_path,
        )
        report = asyncio.run(
            run_packaged_conformance_report(request)
        )
    except Exception:
        _write_status({"status": "failed"}, sys.stderr)
        return 1
    _write_status(
        {
            "adapters": len(report.adapters),
            "scenarios": len(report.scenarios),
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
        "--timeout-seconds",
        required=True,
        type=int,
    )
    parser.add_argument(
        "--result-path",
        required=True,
        type=Path,
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
