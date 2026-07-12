# RAG API

Minimal FastAPI foundation managed with [uv](https://docs.astral.sh/uv/).

## Setup

```bash
uv sync --all-groups
cp .env.example .env
uv run uvicorn app.main:app --reload
```

The application uses an environment file only for local development. Do not
commit `.env`; keep credentials such as `OPENAI_API_KEY` there or in the
deployment environment.

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

## PDF benchmark corpus

The local benchmark corpus is entirely synthetic and covers the PDF classes in
`docs/benchmarks/fixture-manifest.json`. It is not a production scale test.

```bash
uv run python benchmarks/generate_pdf_fixtures.py --font-path /path/to/NotoSansDevanagari-Regular.ttf
uv run python benchmarks/validate_fixture_manifest.py
uv run python benchmarks/run_native_parser_benchmark.py --max-bytes 1024
```
