"""Run one explicitly authorized live provider smoke."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from app.cli.harness import (
    ProviderSmokeLaunchRequest,
    authorize_provider_smoke,
    run_provider_smoke,
)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _parser()
    parsed = parser.parse_args(arguments)
    launch = ProviderSmokeLaunchRequest(
        acknowledged=parsed.acknowledge_live_costs,
        provider=parsed.provider,
        model=parsed.model,
        max_output_tokens=parsed.max_output_tokens,
        timeout_seconds=parsed.timeout_seconds,
        cost_cap_microusd=parsed.cost_cap_microusd,
        config_path=parsed.config_path,
        database_path=parsed.database_path,
        result_path=parsed.result_path,
        destination_url=parsed.destination_url,
        environment_variable=parsed.environment_variable,
        openrouter_policy_path=parsed.openrouter_policy_path,
    )
    try:
        authorized = authorize_provider_smoke(launch)
        asyncio.run(run_provider_smoke(authorized))
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
            "Run one bounded provider smoke. Credential bytes are read only "
            "from the explicitly named environment variable."
        )
    )
    parser.add_argument(
        "--acknowledge-live-costs",
        action="store_true",
        help="Acknowledge that this command makes one billable provider call.",
    )
    parser.add_argument(
        "--provider",
        required=True,
        choices=("openai", "openrouter"),
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-output-tokens", required=True, type=int)
    parser.add_argument("--timeout-seconds", required=True, type=int)
    parser.add_argument("--cost-cap-microusd", required=True, type=int)
    parser.add_argument("--config-path", required=True, type=Path)
    parser.add_argument("--database-path", required=True, type=Path)
    parser.add_argument("--result-path", required=True, type=Path)
    parser.add_argument("--destination-url", required=True)
    parser.add_argument("--environment-variable", required=True)
    parser.add_argument("--openrouter-policy-path", type=Path)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
