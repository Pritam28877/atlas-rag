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

The runner measures object-storage, PostgreSQL catalog, and broker control
paths; it also records vector and lexical recall@1, tenant-filter isolation,
search p95, index-build time, and OpenSearch index bytes. The RabbitMQ control
uses persistent publishing, manual acknowledgement, and a 1,000-message
`reject-publish` queue bound. It validates configuration and a control path,
not a broker-restart durability proof.

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
