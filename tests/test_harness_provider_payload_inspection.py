import asyncio
import hashlib

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import DataClassification
from app.services.harness.providers import (
    DeterministicProviderPayloadInspector,
    ProviderEgressRequest,
    ProviderPayloadInspectionPolicy,
)


def request(body: bytes) -> ProviderEgressRequest:
    return ProviderEgressRequest(
        request_id="req_" + "1" * 32,
        provider="configured-provider",
        credential_handle="pcr_" + "9" * 32,
        target_url="https://api.provider.example/v1/responses",
        classification=DataClassification.CONFIDENTIAL,
        content_type="application/json",
        safe_headers=(),
        body=body,
    )


def policy(
    *,
    denied_body_sha256s: tuple[str, ...] = (),
    maximum_scan_bytes: int = 1_024,
) -> ProviderPayloadInspectionPolicy:
    return ProviderPayloadInspectionPolicy(
        policy_revision_sha256="1" * 64,
        denied_body_sha256s=denied_body_sha256s,
        maximum_scan_bytes=maximum_scan_bytes,
    )


@pytest.mark.parametrize(
    "body",
    [
        b'{"authorization":"Bearer hidden"}',
        b'{"api_key":"hidden"}',
        b"-----BEGIN PRIVATE KEY-----",
        b"AKIA1234567890ABCDEF",
        b"ghp_abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_credential_like_payloads_are_denied(body: bytes) -> None:
    async def scenario() -> None:
        inspector = DeterministicProviderPayloadInspector(policy())

        decision = await inspector.inspect(request(body))

        assert not decision.allowed
        assert decision.policy_revision_sha256 == "1" * 64
        assert body.decode() not in decision.reason

    asyncio.run(scenario())


def test_hash_denylist_size_bound_and_clean_payload() -> None:
    async def scenario() -> None:
        denied_body = b'{"prompt":"deny this exact fixture"}'
        denied_sha256 = hashlib.sha256(denied_body).hexdigest()
        inspector = DeterministicProviderPayloadInspector(
            policy(denied_body_sha256s=(denied_sha256,))
        )

        denied = await inspector.inspect(request(denied_body))
        oversized = await DeterministicProviderPayloadInspector(
            policy(maximum_scan_bytes=4)
        ).inspect(request(b"clean"))
        allowed = await inspector.inspect(request(b'{"prompt":"safe text"}'))

        assert not denied.allowed
        assert not oversized.allowed
        assert allowed.allowed

    asyncio.run(scenario())


def test_dlp_policy_requires_canonical_hash_evidence() -> None:
    first = "1" * 64
    second = "2" * 64
    with pytest.raises(ValidationError, match="unique and sorted"):
        policy(denied_body_sha256s=(second, first))
    with pytest.raises(ValidationError, match="unique and sorted"):
        policy(denied_body_sha256s=(first, first))
