"""Ephemeral quorum-queue topology for the local broker control."""

from __future__ import annotations

import contextlib

import pika

WORK_QUEUE_LIMIT = 16
MESSAGE_TTL_MS = 250


def names(run_id: str) -> dict[str, str]:
    prefix = f"atlas.rag.benchmark.{run_id}"
    return {
        "work_exchange": f"{prefix}.work.exchange",
        "dead_exchange": f"{prefix}.dead.exchange",
        "work_queue": f"{prefix}.work.queue",
        "dead_queue": f"{prefix}.dead.queue",
        "ttl_queue": f"{prefix}.ttl.queue",
        "ttl_dead_queue": f"{prefix}.ttl.dead.queue",
        "overflow_queue": f"{prefix}.overflow.queue",
    }


def declare(channel: pika.channel.Channel, resources: dict[str, str]) -> None:
    channel.exchange_declare(resources["work_exchange"], "direct", durable=True)
    channel.exchange_declare(resources["dead_exchange"], "direct", durable=True)
    channel.queue_declare(
        resources["dead_queue"],
        durable=True,
        arguments={"x-queue-type": "quorum"},
    )
    channel.queue_bind(resources["dead_queue"], resources["dead_exchange"], "dead")
    channel.queue_declare(
        resources["work_queue"],
        durable=True,
        arguments={
            "x-queue-type": "quorum",
            "x-max-length": WORK_QUEUE_LIMIT,
            "x-overflow": "reject-publish",
            "x-dead-letter-exchange": resources["dead_exchange"],
            "x-dead-letter-routing-key": "dead",
        },
    )
    channel.queue_bind(resources["work_queue"], resources["work_exchange"], "work")
    channel.queue_declare(
        resources["ttl_dead_queue"],
        durable=True,
        arguments={"x-queue-type": "quorum"},
    )
    channel.queue_bind(
        resources["ttl_dead_queue"], resources["dead_exchange"], "expired"
    )
    channel.queue_declare(
        resources["ttl_queue"],
        durable=True,
        arguments={
            "x-queue-type": "quorum",
            "x-message-ttl": MESSAGE_TTL_MS,
            "x-dead-letter-exchange": resources["dead_exchange"],
            "x-dead-letter-routing-key": "expired",
        },
    )
    channel.queue_declare(
        resources["overflow_queue"],
        durable=True,
        arguments={
            "x-queue-type": "quorum",
            "x-max-length": 1,
            "x-overflow": "reject-publish",
        },
    )


def cleanup(channel: pika.channel.Channel, resources: dict[str, str]) -> None:
    for queue in (
        "work_queue",
        "dead_queue",
        "ttl_queue",
        "ttl_dead_queue",
        "overflow_queue",
    ):
        with contextlib.suppress(pika.exceptions.ChannelClosedByBroker):
            channel.queue_delete(resources[queue])
    for exchange in ("work_exchange", "dead_exchange"):
        with contextlib.suppress(pika.exceptions.ChannelClosedByBroker):
            channel.exchange_delete(resources[exchange])
