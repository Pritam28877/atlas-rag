"""Run one explicitly authorized Vertex or local-compatible live smoke."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from app.cli.harness import (
    AdapterSmokeLaunchRequest,
    authorize_adapter_smoke,
    run_local_adapter_smoke,
    run_vertex_adapter_smoke,
)


def main(arguments: Sequence[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    request = AdapterSmokeLaunchRequest(
        acknowledged=parsed.acknowledge_live_costs,
        provider=parsed.provider,
        model=parsed.model,
        max_output_tokens=parsed.max_output_tokens,
        timeout_seconds=parsed.timeout_seconds,
        cost_cap_microusd=parsed.cost_cap_microusd,
        disposable_project_id=parsed.disposable_project_id,
        gate_environment_variable=parsed.gate_environment_variable,
        credential_environment_variable=(
            parsed.credential_environment_variable
        ),
        configuration_path=parsed.configuration_path,
        route_policy_path=parsed.route_policy_path,
        identity_path=parsed.identity_path,
        capability_evidence_path=parsed.capability_evidence_path,
        database_path=parsed.database_path,
        result_path=parsed.result_path,
    )
    try:
        authorized = authorize_adapter_smoke(request)
        if authorized.provider == "vertex":
            asyncio.run(run_vertex_adapter_smoke(authorized))
        else:
            asyncio.run(run_local_adapter_smoke(authorized))
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
            "Run exactly one bounded Vertex or local-compatible smoke. "
            "The separate environment gate must contain 'enabled'."
        )
    )
    parser.add_argument(
        "--acknowledge-live-costs",
        action="store_true",
        help="Acknowledge the one live request and admitted provider cost.",
    )
    parser.add_argument(
        "--provider",
        required=True,
        choices=("local-compatible", "vertex"),
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-output-tokens", required=True, type=int)
    parser.add_argument("--timeout-seconds", required=True, type=int)
    parser.add_argument("--cost-cap-microusd", required=True, type=int)
    parser.add_argument("--disposable-project-id")
    parser.add_argument("--gate-environment-variable", required=True)
    parser.add_argument("--credential-environment-variable")
    parser.add_argument(
        "--configuration-path",
        required=True,
        type=Path,
    )
    parser.add_argument("--route-policy-path", required=True, type=Path)
    parser.add_argument("--identity-path", required=True, type=Path)
    parser.add_argument("--capability-evidence-path", type=Path)
    parser.add_argument("--database-path", required=True, type=Path)
    parser.add_argument("--result-path", required=True, type=Path)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
