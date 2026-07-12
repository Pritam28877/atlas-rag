"""Run bounded, synthetic P1.6 controls against local Docker services."""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import platform
import ssl
import statistics
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import boto3
import pika
import psycopg
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env.benchmark"
COMPOSE_PATH = ROOT / "compose.yaml"
RESULTS_PATH = ROOT.parent / "results" / "infrastructure-control.json"
MAX_TRIALS = 20


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", maxsplit=1)
            values[key] = value
    return values


def elapsed(operation: Callable[[], None]) -> float:
    started = time.perf_counter()
    operation()
    return round((time.perf_counter() - started) * 1000, 3)


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * percent))
    return ordered[index]


def run_storage(values: dict[str, str], run_id: str) -> float:
    client = boto3.client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{values['MINIO_API_PORT']}",
        aws_access_key_id=values["MINIO_ROOT_USER"],
        aws_secret_access_key=values["MINIO_ROOT_PASSWORD"],
        region_name="us-east-1",
        config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2}),
    )
    bucket = f"atlas-rag-benchmark-{run_id}"
    key = "synthetic/control.txt"

    def operation() -> None:
        client.create_bucket(Bucket=bucket)
        try:
            client.put_object(
                Bucket=bucket, Key=key, Body=b"synthetic benchmark payload"
            )
            response = client.get_object(Bucket=bucket, Key=key)
            with response["Body"] as body:
                assert body.read() == b"synthetic benchmark payload"
        finally:
            with contextlib.suppress(ClientError):
                client.delete_object(Bucket=bucket, Key=key)
            with contextlib.suppress(ClientError):
                client.delete_bucket(Bucket=bucket)

    try:
        return elapsed(operation)
    finally:
        client.close()


def run_catalog(values: dict[str, str], run_id: str) -> float:
    connection_url = (
        f"postgresql://{values['POSTGRES_USER']}:{values['POSTGRES_PASSWORD']}"
        f"@127.0.0.1:{values['POSTGRES_PORT']}/{values['POSTGRES_DB']}"
    )
    schema = f"bench_{run_id.replace('-', '_')}"

    def operation() -> None:
        with psycopg.connect(
            connection_url, autocommit=True, connect_timeout=10
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cursor.execute(f"CREATE SCHEMA {schema}")
                try:
                    cursor.execute(
                        f"CREATE TABLE {schema}.chunks "
                        "(tenant_id text NOT NULL, body text NOT NULL, "
                        "embedding vector(3) NOT NULL)"
                    )
                    cursor.executemany(
                        f"INSERT INTO {schema}.chunks VALUES (%s, %s, %s)",
                        [
                            ("tenant-a", "synthetic alpha", "[1,0,0]"),
                            ("tenant-b", "synthetic beta", "[0,1,0]"),
                        ],
                    )
                    cursor.execute(
                        f"SELECT body FROM {schema}.chunks "
                        "WHERE tenant_id = %s "
                        "ORDER BY embedding <-> %s::vector LIMIT 1",
                        ("tenant-a", "[1,0,0]"),
                    )
                    assert cursor.fetchone() == ("synthetic alpha",)
                finally:
                    cursor.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")

    return elapsed(operation)


def run_broker(values: dict[str, str], run_id: str) -> float:
    credentials = pika.PlainCredentials(
        values["RABBITMQ_DEFAULT_USER"], values["RABBITMQ_DEFAULT_PASS"]
    )
    parameters = pika.ConnectionParameters(
        host="127.0.0.1",
        port=int(values["RABBITMQ_PORT"]),
        credentials=credentials,
        connection_attempts=2,
        retry_delay=1,
        socket_timeout=10,
        blocked_connection_timeout=30,
    )
    queue = f"atlas.rag.benchmark.{run_id}"

    def operation() -> None:
        with pika.BlockingConnection(parameters) as connection:
            channel = connection.channel()
            try:
                channel.queue_declare(
                    queue=queue,
                    durable=True,
                    arguments={"x-max-length": 1_000, "x-overflow": "reject-publish"},
                )
                channel.confirm_delivery()
                channel.basic_publish(
                    "",
                    queue,
                    b"synthetic control message",
                    properties=pika.BasicProperties(delivery_mode=2),
                )
                method_frame, _, body = channel.basic_get(queue, auto_ack=False)
                assert method_frame is not None
                assert body == b"synthetic control message"
                channel.basic_ack(method_frame.delivery_tag)
            finally:
                with contextlib.suppress(pika.exceptions.ChannelClosedByBroker):
                    channel.queue_delete(queue=queue)

    return elapsed(operation)


def request_json(
    values: dict[str, str], method: str, path: str, payload: Any = None
) -> Any:
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


def run_search(values: dict[str, str], run_id: str) -> dict[str, float | int | bool]:
    index = f"atlas-rag-benchmark-{run_id}"

    def build_index() -> None:
        request_json(
            values,
            "PUT",
            f"/{index}",
            {
                "settings": {"index.knn": True},
                "mappings": {
                    "properties": {
                        "tenant_id": {"type": "keyword"},
                        "body": {"type": "text"},
                        "embedding": {"type": "knn_vector", "dimension": 3},
                    }
                },
            },
        )
        request_json(
            values,
            "PUT",
            f"/{index}/_doc/a?refresh=true",
            {
                "tenant_id": "tenant-a",
                "body": "synthetic alpha",
                "embedding": [1, 0, 0],
            },
        )
        request_json(
            values,
            "PUT",
            f"/{index}/_doc/b?refresh=true",
            {"tenant_id": "tenant-b", "body": "synthetic beta", "embedding": [0, 1, 0]},
        )

    def first_hit(query: dict[str, Any]) -> str:
        response = request_json(
            values,
            "POST",
            f"/{index}/_search",
            {"size": 1, "query": query},
        )
        return response["hits"]["hits"][0]["_id"]

    try:
        build_ms = elapsed(build_index)
        started = time.perf_counter()
        vector_hits = [
            first_hit(
                {
                    "knn": {
                        "embedding": {
                            "vector": vector,
                            "k": 1,
                            "filter": {"term": {"tenant_id": tenant_id}},
                        }
                    }
                }
            )
            for tenant_id, vector in (("tenant-a", [1, 0, 0]), ("tenant-b", [0, 1, 0]))
        ]
        lexical_hits = [
            first_hit(
                {
                    "bool": {
                        "filter": [{"term": {"tenant_id": tenant_id}}],
                        "must": [{"match": {"body": body}}],
                    }
                }
            )
            for tenant_id, body in (
                ("tenant-a", "synthetic alpha"),
                ("tenant-b", "synthetic beta"),
            )
        ]
        query_ms = round((time.perf_counter() - started) * 1000, 3)
        stats = request_json(values, "GET", f"/{index}/_stats/store")
        expected = ["a", "b"]
        vector_recall_at_1 = sum(
            hit == expected[index] for index, hit in enumerate(vector_hits)
        ) / len(expected)
        lexical_recall_at_1 = sum(
            hit == expected[index] for index, hit in enumerate(lexical_hits)
        ) / len(expected)
        return {
            "build_ms": build_ms,
            "query_ms": query_ms,
            "vector_recall_at_1": vector_recall_at_1,
            "lexical_recall_at_1": lexical_recall_at_1,
            "tenant_filter_isolated": (
                vector_hits == expected and lexical_hits == expected
            ),
            "index_storage_bytes": stats["_all"]["primaries"]["store"]["size_in_bytes"],
        }
    finally:
        request_json(values, "DELETE", f"/{index}")


def image_metadata(values: dict[str, str]) -> Any:
    output = subprocess.check_output(
        [
            "docker",
            "compose",
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.trials <= MAX_TRIALS:
        raise SystemExit(f"--trials must be between 1 and {MAX_TRIALS}")
    values = load_env()
    timings: dict[str, list[float]] = {
        "storage": [],
        "catalog": [],
        "broker": [],
        "search_query": [],
        "search_index_build": [],
    }
    search_metrics: list[dict[str, float | int | bool]] = []
    for _ in range(args.trials):
        run_id = uuid.uuid4().hex
        timings["storage"].append(run_storage(values, run_id))
        timings["catalog"].append(run_catalog(values, run_id))
        timings["broker"].append(run_broker(values, run_id))
        metrics = run_search(values, run_id)
        timings["search_query"].append(float(metrics["query_ms"]))
        timings["search_index_build"].append(float(metrics["build_ms"]))
        search_metrics.append(metrics)
    summary = {
        name: {
            "p50_ms": statistics.median(samples),
            "p95_ms": percentile(samples, 0.95),
        }
        for name, samples in timings.items()
    }
    result = {
        "scope": (
            "local synthetic control only; not a production SLO or provider selection"
        ),
        "trials": args.trials,
        "host": {"platform": platform.platform(), "cpu_count": os.cpu_count()},
        "images": image_metadata(values),
        "workload": {
            "documents": 2,
            "searches_per_trial": 4,
            "note": (
                "Small control corpus; projected-volume capacity remains "
                "a production gate."
            ),
        },
        "search": {
            "vector_recall_at_1": min(
                float(metric["vector_recall_at_1"]) for metric in search_metrics
            ),
            "lexical_recall_at_1": min(
                float(metric["lexical_recall_at_1"]) for metric in search_metrics
            ),
            "tenant_filter_isolated": all(
                bool(metric["tenant_filter_isolated"]) for metric in search_metrics
            ),
            "max_index_storage_bytes": max(
                int(metric["index_storage_bytes"]) for metric in search_metrics
            ),
        },
        "broker": {
            "delivery": "persistent publish with manual acknowledgement",
            "queue_bound": {"max_messages": 1_000, "overflow": "reject-publish"},
        },
        "summary": summary,
    }
    RESULTS_PATH.parent.mkdir(exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
