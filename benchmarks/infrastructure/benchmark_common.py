"""Shared bounded helpers for local-only infrastructure controls."""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import ssl
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env.benchmark"
COMPOSE_PATH = ROOT / "compose.yaml"
MAX_TRIALS = 20


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", maxsplit=1)
            values[key] = value
    require_loopback(values)
    return values


def require_loopback(values: dict[str, str]) -> None:
    address = values.get("BENCHMARK_BIND_ADDRESS")
    if address is None:
        raise ValueError("BENCHMARK_BIND_ADDRESS is required")
    try:
        is_loopback = ipaddress.ip_address(address).is_loopback
    except ValueError as error:
        raise ValueError(
            "BENCHMARK_BIND_ADDRESS must be a loopback IP address"
        ) from error
    if not is_loopback:
        raise ValueError("BENCHMARK_BIND_ADDRESS must be a loopback IP address")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def bounded_trials(value: str) -> int:
    trials = int(value)
    if not 1 <= trials <= MAX_TRIALS:
        raise argparse.ArgumentTypeError(f"trials must be between 1 and {MAX_TRIALS}")
    return trials


def elapsed(operation: Callable[[], None]) -> float:
    started = time.perf_counter()
    operation()
    return round((time.perf_counter() - started) * 1000, 3)


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * percent))
    return ordered[index]


def latency_summary(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": statistics.median(values),
        "p95_ms": percentile(values, 0.95),
    }


def request_json(
    values: dict[str, str], method: str, path: str, payload: Any = None
) -> Any:
    require_loopback(values)
    password = values["OPENSEARCH_INITIAL_ADMIN_PASSWORD"]
    authorization = base64.b64encode(f"admin:{password}".encode()).decode()
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        f"https://127.0.0.1:{values['OPENSEARCH_PORT']}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Basic {authorization}",
            "Content-Type": "application/json",
        },
    )
    with urlopen(
        request, context=ssl._create_unverified_context(), timeout=30
    ) as response:
        body = response.read()
        return json.loads(body) if body else None


def wait_for_opensearch(values: dict[str, str], timeout_seconds: float = 30) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            health = request_json(
                values,
                "GET",
                "/_cluster/health?wait_for_status=yellow&timeout=1s",
            )
            if health["status"] in {"yellow", "green"}:
                return
        except (HTTPError, URLError, ConnectionResetError) as error:
            last_error = error
        time.sleep(0.25)
    raise RuntimeError(
        "OpenSearch did not become authenticated and ready"
    ) from last_error
