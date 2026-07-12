"""Object storage and catalog controls for the local P1.6 stack."""

from __future__ import annotations

import base64
import contextlib
import hashlib

import boto3
import psycopg

try:
    from .benchmark_common import elapsed, require
except ImportError:  # Direct script execution keeps this benchmark self-contained.
    from benchmark_common import elapsed, require
from botocore.config import Config
from botocore.exceptions import ClientError


def run_storage(values: dict[str, str], run_id: str) -> float:
    client = boto3.client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{values['MINIO_API_PORT']}",
        aws_access_key_id=values["MINIO_ROOT_USER"],
        aws_secret_access_key=values["MINIO_ROOT_PASSWORD"],
        region_name="us-east-1",
        config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2}),
    )
    bucket = f"atlas-rag-benchmark-{run_id}"
    key = f"documents/{run_id}/source.pdf"
    payload = b"synthetic benchmark payload"
    checksum = hashlib.sha256(payload).hexdigest()

    def operation() -> None:
        client.create_bucket(Bucket=bucket)
        try:
            client.put_bucket_lifecycle_configuration(
                Bucket=bucket,
                LifecycleConfiguration={
                    "Rules": [
                        {
                            "ID": "benchmark-expiry",
                            "Status": "Enabled",
                            "Filter": {"Prefix": "documents/"},
                            "Expiration": {"Days": 1},
                        }
                    ]
                },
            )
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=payload,
                ChecksumSHA256=base64.b64encode(bytes.fromhex(checksum)).decode(),
                Metadata={"sha256": checksum},
            )
            response = client.get_object(Bucket=bucket, Key=key)
            with response["Body"] as body:
                require(body.read() == payload, "storage read did not match upload")
            require(
                response["Metadata"]["sha256"] == checksum,
                "storage checksum metadata did not match upload",
            )
            lifecycle = client.get_bucket_lifecycle_configuration(Bucket=bucket)
            require(
                lifecycle["Rules"][0]["Expiration"] == {"Days": 1},
                "storage lifecycle was not retained",
            )
        finally:
            with contextlib.suppress(ClientError):
                client.delete_object(Bucket=bucket, Key=key)
            with contextlib.suppress(ClientError):
                client.delete_bucket(Bucket=bucket)

    try:
        return elapsed(operation)
    finally:
        client.close()


def run_catalog(values: dict[str, str], run_id: str) -> float:
    connection_url = (
        f"postgresql://{values['POSTGRES_USER']}:{values['POSTGRES_PASSWORD']}"
        f"@127.0.0.1:{values['POSTGRES_PORT']}/{values['POSTGRES_DB']}"
    )
    schema = f"bench_{run_id.replace('-', '_')}"

    def operation() -> None:
        with psycopg.connect(
            connection_url, autocommit=True, connect_timeout=10
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cursor.execute(f"CREATE SCHEMA {schema}")
                try:
                    cursor.execute(
                        f"CREATE TABLE {schema}.chunks "
                        "(tenant_id text NOT NULL, collection_id text NOT NULL, "
                        "body text NOT NULL, search_vector tsvector "
                        "GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED, "
                        "embedding vector(3) NOT NULL)"
                    )
                    cursor.execute(
                        f"CREATE INDEX chunks_search_gin ON {schema}.chunks "
                        "USING GIN (search_vector)"
                    )
                    cursor.executemany(
                        f"INSERT INTO {schema}.chunks "
                        "(tenant_id, collection_id, body, embedding) "
                        "VALUES (%s, %s, %s, %s)",
                        [
                            ("tenant-a", "collection-a", "synthetic alpha", "[1,0,0]"),
                            ("tenant-a", "collection-b", "synthetic beta", "[0,1,0]"),
                            ("tenant-b", "collection-a", "synthetic alpha", "[1,0,0]"),
                        ],
                    )
                    cursor.execute(
                        f"SELECT body FROM {schema}.chunks WHERE tenant_id = %s "
                        "AND collection_id = %s AND search_vector @@ plainto_tsquery("
                        "'simple', %s) ORDER BY embedding <-> %s::vector LIMIT 1",
                        ("tenant-a", "collection-a", "alpha", "[1,0,0]"),
                    )
                    require(
                        cursor.fetchone() == ("synthetic alpha",),
                        "catalog tenant/collection search did not return expected row",
                    )
                finally:
                    cursor.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")

    return elapsed(operation)
