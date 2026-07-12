"""Bounded RabbitMQ quorum-queue contract control for P1.6."""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from collections.abc import Callable
from typing import Any

import pika

try:
    from .benchmark_common import COMPOSE_PATH, ENV_PATH, require
    from .broker_topology import cleanup, declare
    from .broker_topology import names as topology_names
except ImportError:  # Direct script execution keeps this benchmark self-contained.
    from benchmark_common import COMPOSE_PATH, ENV_PATH, require
    from broker_topology import cleanup, declare
    from broker_topology import names as topology_names

PREFETCH_COUNT = 1
MAX_OVERFLOW_PUBLISHES = 20
FORBIDDEN_MESSAGE_KEYS = {"body", "pdf", "text", "url", "path", "secret"}


def _parameters(values: dict[str, str]) -> pika.ConnectionParameters:
    credentials = pika.PlainCredentials(
        values["RABBITMQ_DEFAULT_USER"], values["RABBITMQ_DEFAULT_PASS"]
    )
    return pika.ConnectionParameters(
        host="127.0.0.1",
        port=int(values["RABBITMQ_PORT"]),
        credentials=credentials,
        connection_attempts=2,
        retry_delay=1,
        socket_timeout=10,
        blocked_connection_timeout=30,
    )


def _message(run_id: str) -> tuple[bytes, pika.BasicProperties]:
    payload = {
        "schema_version": "1.0",
        "message_id": str(uuid.uuid4()),
        "tenant_id": f"tenant-{run_id}",
        "collection_id": f"collection-{run_id}",
        "document_id": str(uuid.uuid4()),
        "document_version_id": str(uuid.uuid4()),
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    _assert_message_contract(body)
    return body, pika.BasicProperties(content_type="application/json", delivery_mode=2)


def _assert_message_contract(body: bytes) -> None:
    require(len(body) <= 512, "broker message exceeds the 512-byte contract")
    payload = json.loads(body)
    require(
        set(payload)
        == {
            "schema_version",
            "message_id",
            "tenant_id",
            "collection_id",
            "document_id",
            "document_version_id",
        },
        "broker message keys do not match the ID-only contract",
    )
    require(
        not FORBIDDEN_MESSAGE_KEYS & set(payload),
        "broker message contains a forbidden payload field",
    )
    for key in ("message_id", "document_id", "document_version_id"):
        uuid.UUID(payload[key])


def _publish(
    channel: pika.channel.Channel,
    exchange: str,
    routing_key: str,
    body: bytes,
    properties: pika.BasicProperties,
) -> None:
    channel.basic_publish(exchange, routing_key, body, properties=properties)


def _wait_for_message(
    channel: pika.channel.Channel, queue: str, timeout_seconds: float = 5
) -> tuple[Any, Any, bytes]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        method, properties, body = channel.basic_get(queue, auto_ack=False)
        if method is not None:
            return method, properties, body
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {queue}")


def _redelivery(
    parameters: pika.ConnectionParameters,
    names: dict[str, str],
    body: bytes,
    properties: pika.BasicProperties,
) -> None:
    with pika.BlockingConnection(parameters) as connection:
        channel = connection.channel()
        channel.confirm_delivery()
        _publish(channel, names["work_exchange"], "work", body, properties)
    with pika.BlockingConnection(parameters) as connection:
        channel = connection.channel()
        method, _, received = _wait_for_message(channel, names["work_queue"])
        require(received == body, "broker changed the published message body")
        require(not method.redelivered, "first delivery was incorrectly redelivered")
    with pika.BlockingConnection(parameters) as connection:
        channel = connection.channel()
        method, _, received = _wait_for_message(channel, names["work_queue"])
        require(received == body, "redelivery changed the published message body")
        require(method.redelivered, "unacknowledged message was not redelivered")
        channel.basic_ack(method.delivery_tag)


def _prefetch(
    connection: pika.BlockingConnection,
    channel: pika.channel.Channel,
    names: dict[str, str],
    make_message: Callable[[], tuple[bytes, pika.BasicProperties]],
) -> int:
    channel.basic_qos(prefetch_count=PREFETCH_COUNT)
    for _ in range(2):
        body, properties = make_message()
        _publish(channel, names["work_exchange"], "work", body, properties)
    deliveries: list[int] = []

    def on_delivery(_: pika.channel.Channel, method: Any, *__: Any) -> None:
        deliveries.append(method.delivery_tag)

    consumer_tag = channel.basic_consume(
        names["work_queue"], on_delivery, auto_ack=False
    )
    connection.process_data_events(time_limit=0.5)
    require(
        len(deliveries) == PREFETCH_COUNT,
        "broker did not enforce the per-consumer prefetch limit",
    )
    channel.basic_nack(deliveries[0], requeue=True)
    channel.basic_cancel(consumer_tag)
    for _ in range(2):
        method, _, _ = _wait_for_message(channel, names["work_queue"])
        channel.basic_ack(method.delivery_tag)
    return len(deliveries)


def _ttl(
    channel: pika.channel.Channel,
    names: dict[str, str],
    body: bytes,
    properties: pika.BasicProperties,
) -> None:
    _publish(channel, "", names["ttl_queue"], body, properties)
    method, headers, received = _wait_for_message(channel, names["ttl_dead_queue"])
    require(received == body, "dead-lettered message body changed")
    require(
        bool(headers.headers) and headers.headers["x-death"][0]["reason"] == "expired",
        "TTL message did not reach the DLQ as expired",
    )
    channel.basic_ack(method.delivery_tag)


def _overflow(
    channel: pika.channel.Channel,
    names: dict[str, str],
    make_message: Callable[[], tuple[bytes, pika.BasicProperties]],
) -> None:
    rejected = False
    for _ in range(MAX_OVERFLOW_PUBLISHES):
        body, properties = make_message()
        try:
            _publish(channel, "", names["overflow_queue"], body, properties)
        except pika.exceptions.NackError:
            rejected = True
            break
    require(rejected, "quorum queue did not reject a bounded publish burst")
    declared = channel.queue_declare(names["overflow_queue"], passive=True)
    require(
        1 <= declared.method.message_count < MAX_OVERFLOW_PUBLISHES,
        "quorum queue backlog exceeded the bounded publish window",
    )
    while True:
        method, _, _ = channel.basic_get(names["overflow_queue"], auto_ack=False)
        if method is None:
            break
        channel.basic_ack(method.delivery_tag)


def _restart_rabbitmq(values: dict[str, str]) -> None:
    subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            "atlas-rag-bench",
            "--env-file",
            str(ENV_PATH),
            "-f",
            str(COMPOSE_PATH),
            "restart",
            "rabbitmq",
        ],
        check=True,
        timeout=60,
    )
    subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            "atlas-rag-bench",
            "--env-file",
            str(ENV_PATH),
            "-f",
            str(COMPOSE_PATH),
            "--profile",
            "controls",
            "up",
            "-d",
            "--wait",
            "rabbitmq",
        ],
        check=True,
        timeout=60,
    )


def run_broker_contract(
    values: dict[str, str], run_id: str, restart: bool = False
) -> dict[str, Any]:
    parameters = _parameters(values)
    names = topology_names(run_id)
    started = time.perf_counter()
    with pika.BlockingConnection(parameters) as connection:
        channel = connection.channel()
        channel.confirm_delivery()
        declare(channel, names)
    try:
        body, properties = _message(run_id)
        _redelivery(parameters, names, body, properties)
        with pika.BlockingConnection(parameters) as connection:
            channel = connection.channel()
            channel.confirm_delivery()
            prefetch_deliveries = _prefetch(
                connection, channel, names, lambda: _message(run_id)
            )
            ttl_body, ttl_properties = _message(run_id)
            _ttl(channel, names, ttl_body, ttl_properties)
            _overflow(channel, names, lambda: _message(run_id))
        if restart:
            with pika.BlockingConnection(parameters) as connection:
                channel = connection.channel()
                channel.confirm_delivery()
                restart_body, restart_properties = _message(run_id)
                _publish(
                    channel,
                    names["work_exchange"],
                    "work",
                    restart_body,
                    restart_properties,
                )
            _restart_rabbitmq(values)
            with pika.BlockingConnection(parameters) as connection:
                channel = connection.channel()
                method, _, received = _wait_for_message(channel, names["work_queue"])
                require(received == restart_body, "restart lost a confirmed message")
                channel.basic_ack(method.delivery_tag)
    finally:
        with pika.BlockingConnection(parameters) as connection:
            cleanup(connection.channel(), names)
    return {
        "queue_type": "quorum-single-node-control",
        "message_contract": "persistent ID-only JSON <=512 bytes",
        "prefetch_count": PREFETCH_COUNT,
        "prefetch_deliveries_before_ack": prefetch_deliveries,
        "redelivery": True,
        "ttl_to_dlq": True,
        "overflow_nack_after_bounded_overshoot": True,
        "graceful_restart": restart,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
    }
