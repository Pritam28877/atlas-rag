FROM ghcr.io/astral-sh/uv:latest AS uv

FROM ubuntu:24.04 AS base

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
CMD ["celery", "-A", "app.workers.native_worker:celery", "worker", "--loglevel=INFO"]

FROM base AS ocr

USER root
RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        tesseract-ocr=5.3.4-1build5 \
        tesseract-ocr-eng=1:4.1.0-2 \
    && rm -rf /var/lib/apt/lists/*
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
USER ragworker:ragworker
CMD ["celery", "-A", "app.workers.ocr_worker:celery", "worker", "--loglevel=INFO"]
