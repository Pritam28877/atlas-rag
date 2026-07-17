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
Run native, OCR, publication, and lifecycle pools independently so model/index
work cannot consume extraction capacity. A durable topic exchange supports
non-blocking delayed retries on quorum queues. Task messages are JSON objects
containing only tenant, document-version, and job UUIDs.

```bash
uv run python -m app.workers.native_worker
uv run python -m app.workers.ocr_worker
uv run python -m app.workers.publication_worker
uv run python -m app.workers.lifecycle_worker
uv run celery -A app.workers.publication_worker:celery beat --schedule=/tmp/celerybeat-schedule
```

## Authenticated document intake

Production document APIs require an OIDC access token signed with RS256. Set
`AUTH__ISSUER`, `AUTH__AUDIENCE`, and the HTTPS `AUTH__JWKS_URL`; the token must
contain `sub` and the UUID tenant claim configured by `AUTH__TENANT_CLAIM`.
Collection membership in PostgreSQL remains the authorization source of truth,
so a valid token cannot access another collection or tenant.

The intake flow never sends PDF bytes through FastAPI:

1. `POST /v1/collections` creates a bounded, tenant-owned collection.
2. `POST /v1/collections/{collection_id}/documents` requires an
   `Idempotency-Key` and returns a signed object-storage `PUT` target.
3. The client uploads directly with every returned signed header.
4. `POST /v1/document-versions/{version_id}/complete-upload` verifies stored
   ownership metadata, length, SHA-256, MIME, and the bounded `%PDF-` prefix,
   then records and dispatches a preflight job.
5. `GET /v1/document-versions/{version_id}` returns safe progress. Retry,
   reprocess, cancel, and asynchronous deletion use the idempotent
   `/lifecycle` endpoint.

Collection lists use `limit` (maximum 100) and opaque `cursor` pagination.
Status and lifecycle responses exclude object keys, document text, provider
errors, stack traces, and credentials.

The opt-in P4 integration test uses a disposable PostgreSQL database whose name
ends in `_test`, the existing P2 S3-compatible bucket variables, and the P2
RabbitMQ variables. It performs a real signed upload and real API requests:

```bash
TEST_DATABASE_URL=postgresql://user:password@localhost:15432/rag_test \
P2_STORAGE_ENDPOINT_URL=http://127.0.0.1:19000 \
P2_STORAGE_BUCKET_NAME=rag-p2-test \
P2_STORAGE_ACCESS_KEY_ID=replace-me \
P2_STORAGE_SECRET_ACCESS_KEY=replace-me \
P2_STORAGE_USE_TLS=false \
P2_STORAGE_SERVER_SIDE_ENCRYPTION=provider-default \
P2_BROKER_URL=amqp://user:password@127.0.0.1:15672/rag \
P2_BROKER_USE_TLS=false \
uv run pytest -m catalog_integration tests/test_catalog_integration.py
```

## Isolated worker containers

`docker/compose.workers.yml` defines separate native, OCR, publication, and
lifecycle workers plus a reconciliation scheduler. Workers run as an unprivileged
user with a read-only root filesystem, a bounded `tmpfs` job directory,
PID/memory/CPU limits, and one configured queue each. Start the local platform
first so it creates the named internal `atlas-rag-ingestion` network, then start
the worker compose project, which attaches to that external network. Do not add
a public egress route. The API process never parses PDFs or starts Celery workers.

The native worker uses `pypdf` behind a parser interface. It downloads one
checksum-addressed original into a private job directory with a hard byte
limit, performs page-count, encryption, active-content, content-stream, and
output-limit checks, and writes page records incrementally as JSONL. Native
pages retain page numbers, text blocks/bounds where available, language and
quality signals, parser version, and warnings. Mixed or scanned pages create a
durable OCR job on the isolated OCR queue; native-only documents publish an
immutable normalized artifact and advance to chunking. Expired leases are
reclaimable and duplicate deliveries reuse the same durable artifacts/jobs.

`STORAGE__SERVER_SIDE_ENCRYPTION=provider-default` is permitted only for an
explicitly secured development/test provider such as the local MinIO harness.
Production configuration rejects that mode and requires `AES256` or `aws:kms`.

OCR uses the benchmark-approved Tesseract 5.3.4 English profile and Poppler
24.02.0 renderer in the separate OCR image/queue. It renders only page numbers
persisted as OCR-eligible, one page at a time at the configured DPI. Per-page
image bytes, subprocess time, OCR blocks, text output, worker concurrency,
prefetch, memory, CPU, and tasks-per-child are bounded. OCR word confidence and
pixel bounds are stored with page provenance; native pages are retained during
mixed-document merge. Unsupported languages, no text, low confidence, tool
timeouts, incomplete page coverage, and retry exhaustion have explicit safe
reason codes. Process-local OCR diagnostic counters use fixed labels, but the
current API metrics endpoint does not aggregate worker-process registries.

The selected v1 OCR profile does not claim handwriting, table reconstruction,
non-English OCR, or reliable reading order for complex layouts. Those inputs
remain native-with-warning or explicit OCR failure/review outcomes until a
separate benchmark approves another profile.

The publication worker runs three durable stages: deterministic page-aware
chunking, bounded local embedding, and hybrid index publication. Chunk identity
is derived from tenant, document version, chunker profile, ordinal, and text
SHA-256. Each immutable JSONL record retains page range, character and block
offsets where available, token count, section path, chunker version, and safe
text. Pages never merge across citation boundaries; per-page characters,
document chunks, overlap, batch size, worker concurrency, task lifetime, and
process recycling are configuration-bounded.

The selected embedding profile is FastEmbed 0.8.0 mean pooling with the baked,
checksum-verified multilingual MiniLM ONNX snapshot documented in
`docs/benchmarks/results-embedding-opensearch.md`. Workers never download the
model while processing a task. OpenSearch 2.19.5 receives staged records with
tenant, collection, document-version, page, hash, lexical text, and vector
fields. Records become searchable only after every expected chunk is verified;
PostgreSQL independently reconciles exact chunk, embedding, lexical, vector,
and manifest evidence before allowing `ready`. Replayed jobs use deterministic
IDs and upserts rather than duplicate chunks or search records.

Reprocessing requires an explicit new `pipeline_profile` and creates a linked,
immutable document version from the prior normalized artifact. The prior version
remains searchable until complete replacement publication, then promotion
supersedes and de-indexes it. Deletion first revokes tenant/version-scoped search
records, cancels active jobs, and then removes managed artifacts when retention
and legal hold permit. The publication worker also exposes a bounded
`app.workers.publication.reconcile` task for stale leases, stale dispatches,
incomplete publication evidence, and retention-delayed cleanup. Alert rules,
operator procedures, and measured limits are under `docs/operations/` and
`docs/benchmarks/results-lifecycle-hardening.md`.

The opt-in P7 integration requires the disposable PostgreSQL/S3/OpenSearch
settings and an already downloaded or image-baked model directory:

```bash
TEST_DATABASE_URL=postgresql://user:password@localhost:15432/rag_test \
P2_STORAGE_ENDPOINT_URL=http://127.0.0.1:19000 \
P2_STORAGE_BUCKET_NAME=rag-p2-test \
P2_STORAGE_ACCESS_KEY_ID=replace-me \
P2_STORAGE_SECRET_ACCESS_KEY=replace-me \
P2_STORAGE_USE_TLS=false \
P2_STORAGE_SERVER_SIDE_ENCRYPTION=provider-default \
P2_SEARCH_ENDPOINT_URL=https://127.0.0.1:19200 \
P2_SEARCH_USERNAME=admin \
P2_SEARCH_PASSWORD=replace-me \
P2_SEARCH_VERIFY_TLS=false \
P2_EMBEDDING_MODEL_DIRECTORY=/opt/rag/models/multilingual-minilm \
uv run pytest -m publication_integration tests/test_publication_integration.py
```

After explicitly starting local MinIO and RabbitMQ, run the opt-in P2 transfer
and broker checks with a pre-created empty bucket:

```bash
P2_STORAGE_ENDPOINT_URL=http://127.0.0.1:19000 \
P2_STORAGE_BUCKET_NAME=rag-p2-test \
P2_STORAGE_ACCESS_KEY_ID=replace-me \
P2_STORAGE_SECRET_ACCESS_KEY=replace-me \
P2_STORAGE_USE_TLS=false \
P2_STORAGE_SERVER_SIDE_ENCRYPTION=provider-default \
P2_BROKER_URL=amqp://user:password@127.0.0.1:15672/rag \
P2_BROKER_USE_TLS=false \
uv run pytest -m platform_integration tests/test_platform_integration.py
```

Use the isolated P2 local harness rather than the P1 benchmark stack. Generate
fresh local-only credentials, export the variables shown above plus
`P2_POSTGRES_USER`, `P2_POSTGRES_PASSWORD`, `P2_RABBITMQ_USER`,
`P2_RABBITMQ_PASSWORD`, and
`P2_OPENSEARCH_PASSWORD`, then run:

```bash
docker compose -f docker/compose.local-platform.yml up -d --wait
uv run python scripts/create_local_p2_bucket.py
uv run pytest -m platform_integration tests/test_platform_integration.py
docker compose -f docker/compose.local-platform.yml down -v --remove-orphans
```

For the real worker acceptance path, keep the platform running, export the same
service settings for `docker/compose.workers.yml`, set
`SEARCH__INDEX_NAME=rag-worker-e2e`, and start all five worker services. Then set
the host-side test variables above plus
`P2_WORKER_SEARCH_INDEX_NAME=rag-worker-e2e` and run:

```bash
docker compose -f docker/compose.workers.yml up -d --build
uv run pytest -m worker_integration tests/test_worker_runtime_integration.py
docker compose -f docker/compose.workers.yml down --remove-orphans
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
