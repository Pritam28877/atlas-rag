FROM ghcr.io/astral-sh/uv:0.8.0@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv

FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90 AS base

ARG DEBIAN_FRONTEND=noninteractive

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

COPY --from=uv /uv /uvx /bin/
RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates python3.12 python3.12-venv \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ragworker \
    && useradd --uid 10001 --gid ragworker --no-create-home --shell /usr/sbin/nologin ragworker

WORKDIR /app
COPY pyproject.toml README.md uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python /usr/bin/python3.12
COPY app ./app
RUN uv sync --frozen --no-dev --python /usr/bin/python3.12

FROM base AS native

USER ragworker:ragworker
CMD ["python", "-m", "app.workers.native_worker"]

FROM base AS lifecycle

USER ragworker:ragworker
CMD ["python", "-m", "app.workers.lifecycle_worker"]

FROM base AS scheduler

USER ragworker:ragworker
CMD ["celery", "-A", "app.workers.publication_worker:celery", "beat", "--loglevel=INFO", "--schedule=/tmp/celerybeat-schedule"]

FROM base AS ocr

USER root
RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        poppler-utils=24.02.0-1ubuntu9.9 \
        tesseract-ocr=5.3.4-1build5 \
        tesseract-ocr-eng=1:4.1.0-2 \
    && rm -rf /var/lib/apt/lists/*
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
USER ragworker:ragworker
CMD ["python", "-m", "app.workers.ocr_worker"]

FROM base AS publication

USER root
RUN mkdir -p /opt/rag/models/multilingual-minilm \
    && python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q', revision='faf4aa4225822f3bc6376869cb1164e8e3feedd0', local_dir='/opt/rag/models/multilingual-minilm', allow_patterns=['config.json','model_optimized.onnx','special_tokens_map.json','tokenizer.json','tokenizer_config.json'])" \
    && echo "634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99  /opt/rag/models/multilingual-minilm/model_optimized.onnx" | sha256sum --check --strict \
    && chown -R ragworker:ragworker /opt/rag/models
USER ragworker:ragworker
CMD ["python", "-m", "app.workers.publication_worker"]
