"""Run bounded, fixture-derived P1.6 controls against local Docker services."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import uuid
from pathlib import Path
from typing import Any

try:
    from .benchmark_common import (
        COMPOSE_PATH,
        ENV_PATH,
        bounded_trials,
        latency_summary,
        load_env,
        wait_for_opensearch,
    )
    from .broker_contract import run_broker_contract
    from .retrieval_control import run_retrieval_control
    from .storage_catalog import run_catalog, run_storage
except ImportError:  # Direct script execution keeps this benchmark self-contained.
    from benchmark_common import (
        COMPOSE_PATH,
        ENV_PATH,
        bounded_trials,
        latency_summary,
        load_env,
        wait_for_opensearch,
    )
    from broker_contract import run_broker_contract
    from retrieval_control import run_retrieval_control
    from storage_catalog import run_catalog, run_storage

ROOT = Path(__file__).resolve().parent
RESULTS_PATH = ROOT.parent / "results" / "infrastructure-control.json"


def image_metadata() -> Any:
    output = subprocess.check_output(
        [
            "docker",
            "compose",
            "--project-name",
            "atlas-rag-bench",
            "--env-file",
            str(ENV_PATH),
            "-f",
            str(COMPOSE_PATH),
            "images",
            "--format",
            "json",
        ],
        text=True,
        timeout=30,
    )
    return json.loads(output) if output else []


def aggregate_retrieval(records: list[dict[str, Any]]) -> dict[str, Any]:
    modes = sorted({record["mode"] for record in records})
    aggregate: dict[str, Any] = {}
    for mode in modes:
        mode_records = [record for record in records if record["mode"] == mode]
        metrics = mode_records[0]["metrics"]
        aggregate[mode] = {
            "latency": latency_summary(
                [float(record["latency_ms"]) for record in mode_records]
            ),
            "metrics": {
                name: (
                    None
                    if value is None
                    else sum(float(record["metrics"][name]) for record in mode_records)
                    / len(mode_records)
                )
                for name, value in metrics.items()
                if name not in {"scope_isolated", "metadata_integrity"}
            },
            "scope_isolated": all(
                bool(record["metrics"]["scope_isolated"]) for record in mode_records
            ),
            "metadata_integrity": all(
                bool(record["metrics"]["metadata_integrity"]) for record in mode_records
            ),
        }
    return aggregate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=bounded_trials, default=5)
    parser.add_argument("--broker-restart", action="store_true")
    args = parser.parse_args()
    values = load_env()
    wait_for_opensearch(values)
    timings: dict[str, list[float]] = {"storage": [], "catalog": [], "index_build": []}
    retrieval_records: list[dict[str, Any]] = []
    storage_bytes: list[int] = []
    for _ in range(args.trials):
        run_id = uuid.uuid4().hex
        timings["storage"].append(run_storage(values, run_id))
        timings["catalog"].append(run_catalog(values, run_id))
        retrieval, records = run_retrieval_control(values, run_id)
        timings["index_build"].append(float(retrieval["build_ms"]))
        storage_bytes.append(int(retrieval["index_storage_bytes"]))
        retrieval_records.extend(records)
    broker = run_broker_contract(values, uuid.uuid4().hex, args.broker_restart)
    result = {
        "scope": (
            "local synthetic control only; not a production SLO or provider selection"
        ),
        "trials": args.trials,
        "host": {"platform": platform.platform(), "cpu_count": os.cpu_count()},
        "images": image_metadata(),
        "storage_catalog": {
            "storage": latency_summary(timings["storage"]),
            "catalog": latency_summary(timings["catalog"]),
        },
        "retrieval": {
            "embedding_profile": "unicode-token-hash-v1-d64 (plumbing control only)",
            "hybrid_profile": "application-side reciprocal-rank fusion, k=60",
            "index_build": latency_summary(timings["index_build"]),
            "max_index_storage_bytes": max(storage_bytes),
            "snapshot": retrieval["snapshot"],
            "results": aggregate_retrieval(retrieval_records),
            "raw_per_query": retrieval_records,
            "recall_at_10": "not meaningful for the five-document primary scope",
        },
        "broker": broker,
    }
    RESULTS_PATH.parent.mkdir(exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"retrieval": result["retrieval"]["results"], "broker": broker},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
