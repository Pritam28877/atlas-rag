# Local P1.6 benchmark controls

This folder is benchmark tooling only. It does not run the FastAPI app, Celery,
migrations, or a production deployment. All services bind to loopback and use
synthetic data only. The Docker bridge permits host-to-container loopback
connections; it is not a production network-isolation control.

## Start

```bash
uv run python benchmarks/infrastructure/create_local_env.py
uv run python benchmarks/infrastructure/preflight.py
docker compose --env-file benchmarks/infrastructure/.env.benchmark \
  -f benchmarks/infrastructure/compose.yaml --profile controls --profile search up -d --wait
uv sync --group dev --group infra-benchmark
uv run python benchmarks/infrastructure/run_control_operations.py --trials 5
```

Results are written to the ignored `benchmarks/results/` folder. They must be
read as local synthetic controls only; they do not select production providers,
prove HA, or establish million-document capacity.

See [the retrieval-control protocol](../../docs/benchmarks/retrieval-control.md)
for corpus provenance, qrels limits, and the distinction between this control
and a provider-selection benchmark.

The runner records object-storage and PostgreSQL catalog controls; it evaluates
fixture-derived lexical, deterministic-vector, and harness-level hybrid RRF
retrieval against versioned draft qrels. It records per-query ranking, Recall
and nDCG through @5, citation accuracy, scope isolation, p50/p95 latency,
index-build time, and index bytes. `@10` is explicitly not meaningful for the
five-document primary scope.

The RabbitMQ control uses a single-node quorum queue, ID-only persistent JSON,
manual acknowledgement, per-consumer prefetch `1`, redelivery, TTL-to-DLQ, and
publisher-confirmed `reject-publish` overflow. Quorum queues can overshoot a
length limit before rejecting a publisher, so this confirms bounded rejection
rather than a strict one-message cap. Add `--broker-restart` for an isolated,
graceful broker-restart check; neither mode proves multi-node availability.

The generated credentials file is atomically written with owner-only (`0600`)
permissions. The stack caps total container memory at 3.25 GiB and each
service has a PID limit; the preflight still requires 8 GiB Docker memory for
the host and image/runtime overhead.

## Cleanup

```bash
docker compose --env-file benchmarks/infrastructure/.env.benchmark \
  -f benchmarks/infrastructure/compose.yaml down -v --remove-orphans
```

Never use these volumes, credentials, or images for customer data or P2 runtime
infrastructure.
