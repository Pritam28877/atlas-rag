"""Create ignored credentials for a local-only benchmark stack."""

from __future__ import annotations

import argparse
import os
import secrets
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env.benchmark"


def value(prefix: str) -> str:
    return f"{prefix}-{secrets.token_urlsafe(24)}"


def write_private_env(contents: str) -> None:
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".env.benchmark.", dir=ROOT, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(contents)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, ENV_PATH)
        ENV_PATH.chmod(0o600)
    finally:
        Path(temporary_path).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if ENV_PATH.exists() and not args.force:
        raise SystemExit(f"Refusing to overwrite {ENV_PATH}")
    write_private_env(
        "\n".join(
            [
                "BENCHMARK_BIND_ADDRESS=127.0.0.1",
                f"MINIO_ROOT_USER={value('minio')}",
                f"MINIO_ROOT_PASSWORD={value('minio')}",
                "MINIO_API_PORT=19000",
                "MINIO_CONSOLE_PORT=19001",
                f"POSTGRES_USER={value('postgres')}",
                f"POSTGRES_PASSWORD={value('postgres')}",
                "POSTGRES_DB=atlas_rag_benchmark",
                "POSTGRES_PORT=15432",
                f"RABBITMQ_DEFAULT_USER={value('rabbit')}",
                f"RABBITMQ_DEFAULT_PASS={value('rabbit')}",
                "RABBITMQ_PORT=15672",
                "RABBITMQ_MANAGEMENT_PORT=25672",
                f"OPENSEARCH_INITIAL_ADMIN_PASSWORD={value('opensearch')}",
                "OPENSEARCH_PORT=19200",
                "OPENSEARCH_JAVA_HEAP_MIB=512",
                "",
            ]
        )
    )
    print(f"Created ignored benchmark credentials at {ENV_PATH}")


if __name__ == "__main__":
    main()
