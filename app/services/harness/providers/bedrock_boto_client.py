"""Explicit signed Boto3 Bedrock Runtime client construction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

from app.services.harness.providers.bedrock_credentials import (
    BedrockCredentialMaterial,
)
from app.services.harness.providers.bedrock_policy import (
    AuthorizedBedrockRoute,
)


class BedrockEventStream(Protocol):
    def __iter__(self) -> BedrockEventStream: ...

    def __next__(self) -> dict[str, object]: ...

    def close(self) -> None: ...


class BedrockRuntimeClient(Protocol):
    def converse_stream(
        self,
        **request: object,
    ) -> dict[str, object]: ...

    def close(self) -> None: ...


class BedrockRuntimeClientFactory(Protocol):
    def create(
        self,
        route: AuthorizedBedrockRoute,
        credential: BedrockCredentialMaterial,
        *,
        timeout_seconds: int,
    ) -> BedrockRuntimeClient: ...


class Boto3BedrockRuntimeClientFactory:
    def create(
        self,
        route: AuthorizedBedrockRoute,
        credential: BedrockCredentialMaterial,
        *,
        timeout_seconds: int,
    ) -> BedrockRuntimeClient:
        if not 1 <= timeout_seconds <= 300:
            raise ValueError("Bedrock client timeout is invalid")
        access_key, secret_key, session_token = credential.views()
        client_creator, config_factory = _load_boto3_factories()
        client = client_creator(
            "bedrock-runtime",
            region_name=route.region,
            endpoint_url=route.endpoint_url,
            aws_access_key_id=bytes(access_key).decode("utf-8"),
            aws_secret_access_key=bytes(secret_key).decode("utf-8"),
            aws_session_token=(
                bytes(session_token).decode("utf-8")
                if session_token is not None
                else None
            ),
            config=config_factory(
                connect_timeout=min(timeout_seconds, 10),
                read_timeout=timeout_seconds,
                retries={"max_attempts": 0},
                max_pool_connections=1,
            ),
        )
        return cast(BedrockRuntimeClient, client)


def _load_boto3_factories() -> tuple[
    Callable[..., object],
    Callable[..., object],
]:
    """Load infrastructure SDKs only when an authorized call is dispatched."""
    # Boto3 and Botocore do not publish PEP 561 typing metadata.
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]

    return boto3.client, Config


@dataclass(frozen=True, slots=True)
class BedrockResponseEnvelope:
    stream: BedrockEventStream
    response_metadata_sha256: str | None


def response_event_stream(
    response: dict[str, object],
) -> BedrockResponseEnvelope:
    if "stream" not in response or set(response) - {
        "stream",
        "ResponseMetadata",
    }:
        raise ValueError("Bedrock response envelope is invalid")
    stream = response["stream"]
    if not hasattr(stream, "__iter__") or not hasattr(stream, "close"):
        raise ValueError("Bedrock response stream is invalid")
    metadata = response.get("ResponseMetadata")
    metadata_sha256 = None
    if metadata is not None:
        try:
            encoded = json.dumps(
                metadata,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        except (TypeError, ValueError):
            raise ValueError("Bedrock response metadata is invalid") from None
        if not 1 <= len(encoded) <= 64 * 1024:
            raise ValueError("Bedrock response metadata is invalid")
        metadata_sha256 = hashlib.sha256(encoded).hexdigest()
    return BedrockResponseEnvelope(
        stream=cast(BedrockEventStream, stream),
        response_metadata_sha256=metadata_sha256,
    )
