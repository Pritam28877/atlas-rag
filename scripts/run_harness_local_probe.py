"""Run one explicitly authorized six-call local capability probe."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from app.cli.harness import (
    LocalProbeLaunchRequest,
    authorize_local_probe,
    run_local_capability_probe,
)


def main(arguments: Sequence[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    request = LocalProbeLaunchRequest(
        acknowledged=parsed.acknowledge_live_probe,
        model=parsed.model,
        per_case_timeout_seconds=parsed.per_case_timeout_seconds,
        evidence_ttl_seconds=parsed.evidence_ttl_seconds,
        gate_environment_variable=parsed.gate_environment_variable,
        credential_environment_variable=(
            parsed.credential_environment_variable
        ),
        configuration_path=parsed.configuration_path,
        route_policy_path=parsed.route_policy_path,
        identity_path=parsed.identity_path,
        result_path=parsed.result_path,
    )
    try:
        authorized = authorize_local_probe(request)
        asyncio.run(run_local_capability_probe(authorized))
    except Exception:
        _write_status("failed", sys.stderr)
        return 1
    _write_status("completed", sys.stdout)
    return 0


def _write_status(status: str, stream) -> None:
    print(
        json.dumps(
            {"status": status},
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=stream,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run exactly six bounded local capability requests. "
            "The separate environment gate must contain 'enabled'."
        )
    )
    parser.add_argument(
        "--acknowledge-live-probe",
        action="store_true",
        help="Acknowledge the six live local endpoint requests.",
    )
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--per-case-timeout-seconds",
        required=True,
        type=int,
    )
    parser.add_argument(
        "--evidence-ttl-seconds",
        required=True,
        type=int,
    )
    parser.add_argument("--gate-environment-variable", required=True)
    parser.add_argument("--credential-environment-variable")
    parser.add_argument(
        "--configuration-path",
        required=True,
        type=Path,
    )
    parser.add_argument("--route-policy-path", required=True, type=Path)
    parser.add_argument("--identity-path", required=True, type=Path)
    parser.add_argument("--result-path", required=True, type=Path)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
