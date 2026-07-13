"""Create the explicitly configured local S3 bucket for P2 integration tests."""

import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def required_value(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def main() -> None:
    endpoint_url = required_value("P2_STORAGE_ENDPOINT_URL")
    bucket_name = required_value("P2_STORAGE_BUCKET_NAME")
    access_key_id = required_value("P2_STORAGE_ACCESS_KEY_ID")
    secret_access_key = required_value("P2_STORAGE_SECRET_ACCESS_KEY")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=30),
    )
    try:
        client.create_bucket(Bucket=bucket_name)
    except ClientError as error:
        error_code = error.response.get("Error", {}).get("Code")
        if error_code not in {"BucketAlreadyExists", "BucketAlreadyOwnedByYou"}:
            raise
    finally:
        client.close()


if __name__ == "__main__":
    main()
