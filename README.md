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
