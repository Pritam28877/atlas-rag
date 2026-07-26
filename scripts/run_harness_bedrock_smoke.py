"""Run the explicitly admitted, signed Bedrock live smoke."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from app.cli.harness import (
    BedrockSmokeLaunchRequest,
    admit_bedrock_smoke,
    run_bedrock_smoke,
)


def main(arguments: Sequence[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    try:
        launch = admit_bedrock_smoke(
            BedrockSmokeLaunchRequest(
                acknowledged=parsed.acknowledge_live_costs,
                grant_path=parsed.grant_path,
                configuration_path=parsed.configuration_path,
                route_policy_path=parsed.route_policy_path,
                identity_path=parsed.identity_path,
                database_path=parsed.database_path,
                result_path=parsed.result_path,
                signing_key_environment_variable=(
                    parsed.signing_key_environment_variable
                ),
            )
        )
        asyncio.run(run_bedrock_smoke(launch))
    except Exception:
        print(
            json.dumps(
                {"status": "failed"},
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {"status": "completed"},
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run two signed, cost-bounded Bedrock calls using private "
            "web-identity inputs."
        )
    )
    parser.add_argument(
        "--acknowledge-live-costs",
        action="store_true",
        help="Acknowledge that this command makes two billable AWS calls.",
    )
    parser.add_argument("--grant-path", required=True, type=Path)
    parser.add_argument("--configuration-path", required=True, type=Path)
    parser.add_argument("--route-policy-path", required=True, type=Path)
    parser.add_argument("--identity-path", required=True, type=Path)
    parser.add_argument("--database-path", required=True, type=Path)
    parser.add_argument("--result-path", required=True, type=Path)
    parser.add_argument(
        "--signing-key-environment-variable",
        required=True,
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
