# RAG API

Minimal FastAPI foundation managed with [uv](https://docs.astral.sh/uv/).

## Setup

```bash
uv sync --group dev
cp .env.example .env
uv run uvicorn app.main:app --reload
```

The application uses an environment file only for local development. Do not
commit `.env`; keep credentials such as `OPENAI_API_KEY` there or in the
deployment environment. Put machine-only non-secret overrides such as a local
database socket URL in `.env.local`; it loads after `.env`.

## Layout

```text
app/
  api/       # HTTP routers and request/response models
  core/      # settings, logging, security, shared infrastructure
  services/  # domain behavior, grouped by capability when it exists
```

Create service packages for real RAG capabilities, not in advance:

- `services/ingestion/` — validate, parse, and chunk source documents.
- `services/retrieval/` — embed queries and fetch ranked context.
- `services/generation/` — prompt construction and model invocation.

Routes call services; services do not depend on FastAPI request objects. This
keeps the API transport separate from RAG behavior and makes services testable.

## Verification

```bash
uv run ruff check .
uv run pytest
```

## Database migrations

The local P2 runtime baseline is PostgreSQL 18. The P1 benchmark compose file
remains pinned to its recorded PostgreSQL 16 control image for reproducibility;
it is not the application database. Machine-specific values can live in the
ignored `.env.local` file, which overrides `.env` without replacing its secrets.

Set `DATABASE__URL` to a dedicated local PostgreSQL database. Migration commands
read that environment value through the same typed application settings; the
Alembic configuration contains no credentials.

```bash
uv run alembic upgrade head
uv run alembic downgrade base
```

Use `downgrade base` only against an empty disposable development/test database.
Application database work should use `Database.transaction()` so successful
units commit and exceptions roll back while the session is always closed.

The real lifecycle test is opt-in and refuses any database whose name does not
end in `_test`:

```bash
TEST_DATABASE_URL=postgresql://user:password@localhost:5432/rag_test \
  uv run pytest -m database_integration tests/test_migrations.py
```

## Worker queues

Celery workers require `BROKER__URL`; no worker is started by the API process.
Run native and OCR pools independently so a saturated OCR queue cannot consume
native extraction capacity. Task messages are JSON objects containing only
tenant, document-version, and job UUIDs.

```bash
uv run celery -A app.workers.native_worker:celery worker --queues ingestion.native
uv run celery -A app.workers.ocr_worker:celery worker --queues ingestion.ocr
```

## Isolated worker containers

`docker/compose.workers.yml` defines separate native and OCR worker profiles.
Both run as an unprivileged user with a read-only root filesystem, a bounded
`tmpfs` job directory, PID/memory/CPU limits, and an internal-only network.
Attach only trusted broker, storage, and database services to that internal
network; do not add a public egress route. The API process never parses PDFs or
starts Celery workers.

After explicitly starting local MinIO and RabbitMQ, run the opt-in P2 transfer
and broker checks with a pre-created empty bucket:

```bash
P2_STORAGE_ENDPOINT_URL=http://127.0.0.1:9000 \
P2_STORAGE_BUCKET_NAME=rag-p2-test \
P2_STORAGE_ACCESS_KEY_ID=replace-me \
P2_STORAGE_SECRET_ACCESS_KEY=replace-me \
P2_STORAGE_USE_TLS=false \
P2_BROKER_URL=amqp://user:password@127.0.0.1:5672/rag \
P2_BROKER_USE_TLS=false \
uv run pytest -m platform_integration tests/test_platform_integration.py
```

Use the isolated P2 local harness rather than the P1 benchmark stack. Generate
fresh local-only credentials, export the variables shown above plus
`P2_RABBITMQ_USER` and `P2_RABBITMQ_PASSWORD`, then run:

```bash
docker compose -f docker/compose.local-platform.yml up -d --wait
uv run python scripts/create_local_p2_bucket.py
uv run pytest -m platform_integration tests/test_platform_integration.py
docker compose -f docker/compose.local-platform.yml down -v --remove-orphans
```

## PDF benchmark corpus

The local benchmark corpus is entirely synthetic and covers the PDF classes in
`docs/benchmarks/fixture-manifest.json`. It is not a production scale test.

```bash
uv run python benchmarks/generate_pdf_fixtures.py \
  --font-path /path/to/NotoSansDevanagari-Regular.ttf \
  --latin-font-path /path/to/DejaVuSans.ttf
uv run python benchmarks/validate_fixture_manifest.py
uv run python benchmarks/run_native_parser_benchmark.py --limit-profile-max-bytes 1024
```

Docling OCR/layout benchmarking is opt-in; see
[`docs/benchmarks/ocr-benchmark.md`](docs/benchmarks/ocr-benchmark.md).
