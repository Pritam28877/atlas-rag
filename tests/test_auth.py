import asyncio
import time
from uuid import UUID

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.core.auth import OidcVerifier
from app.core.config import AuthSettings, ProviderTimeoutSettings

TENANT_ID = UUID("a7006ca9-bac4-4702-acaf-1e7c0dd6e7b6")


def create_verifier() -> tuple[OidcVerifier, rsa.RSAPrivateKey, list[str]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(
        private_key.public_key(), as_dict=True
    )
    public_jwk["kid"] = "test-key"

    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        assert request.url == "https://identity.example.test/jwks"
        return httpx.Response(200, json={"keys": [public_jwk]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    settings = AuthSettings(
        issuer="https://identity.example.test/",
        audience="rag-api",
        jwks_url="https://identity.example.test/jwks",
    )
    verifier = OidcVerifier(settings, ProviderTimeoutSettings(), client)
    return verifier, private_key, requests


def create_token(
    private_key: rsa.RSAPrivateKey,
    *,
    tenant_id: str = str(TENANT_ID),
    audience: str = "rag-api",
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "https://identity.example.test/",
            "aud": audience,
            "sub": "user-123",
            "tenant_id": tenant_id,
            "iat": now,
            "exp": now + 300,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


def test_oidc_verifier_accepts_signed_tenant_token() -> None:
    verifier, private_key, _ = create_verifier()

    principal = asyncio.run(verifier.verify(create_token(private_key)))

    assert principal.subject == "user-123"
    assert principal.tenant_id == TENANT_ID


def test_oidc_verifier_rejects_wrong_audience() -> None:
    verifier, private_key, _ = create_verifier()

    with pytest.raises(HTTPException) as captured:
        asyncio.run(verifier.verify(create_token(private_key, audience="other-api")))

    assert captured.value.status_code == 401
    assert captured.value.detail == "invalid bearer token"


def test_oidc_verifier_rejects_invalid_tenant_claim() -> None:
    verifier, private_key, _ = create_verifier()

    with pytest.raises(HTTPException) as captured:
        asyncio.run(
            verifier.verify(create_token(private_key, tenant_id="not-a-uuid"))
        )

    assert captured.value.status_code == 401


def test_oidc_settings_require_complete_https_configuration() -> None:
    with pytest.raises(ValueError, match="configured together"):
        AuthSettings(issuer="https://identity.example.test/")

    with pytest.raises(ValueError, match="HTTPS"):
        AuthSettings(
            issuer="https://identity.example.test/",
            audience="rag-api",
            jwks_url="http://identity.example.test/jwks",
        )


def test_unknown_key_id_forces_only_one_refresh_inside_cooldown() -> None:
    verifier, private_key, requests = create_verifier()
    asyncio.run(verifier.verify(create_token(private_key)))
    now = int(time.time())
    unknown_key_token = jwt.encode(
        {
            "iss": "https://identity.example.test/",
            "aud": "rag-api",
            "sub": "user-123",
            "tenant_id": str(TENANT_ID),
            "iat": now,
            "exp": now + 300,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "attacker-controlled-key-id"},
    )

    with pytest.raises(HTTPException):
        asyncio.run(verifier.verify(unknown_key_token))

    with pytest.raises(HTTPException):
        asyncio.run(verifier.verify(unknown_key_token))

    assert requests == [
        "https://identity.example.test/jwks",
        "https://identity.example.test/jwks",
    ]


def test_unknown_key_refresh_accepts_rotated_signing_key() -> None:
    old_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rotated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    old_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(old_key.public_key(), as_dict=True)
    rotated_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(
        rotated_key.public_key(), as_dict=True
    )
    old_jwk["kid"] = "old-key"
    rotated_jwk["kid"] = "rotated-key"
    request_count = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        selected_key = old_jwk if request_count == 1 else rotated_jwk
        return httpx.Response(200, json={"keys": [selected_key]})

    settings = AuthSettings(
        issuer="https://identity.example.test/",
        audience="rag-api",
        jwks_url="https://identity.example.test/jwks",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    verifier = OidcVerifier(settings, ProviderTimeoutSettings(), client)
    now = int(time.time())

    def token(private_key, key_id):
        return jwt.encode(
            {
                "iss": settings.issuer,
                "aud": settings.audience,
                "sub": "user-123",
                "tenant_id": str(TENANT_ID),
                "iat": now,
                "exp": now + 300,
            },
            private_key,
            algorithm="RS256",
            headers={"kid": key_id},
        )

    asyncio.run(verifier.verify(token(old_key, "old-key")))
    principal = asyncio.run(verifier.verify(token(rotated_key, "rotated-key")))

    assert principal.tenant_id == TENANT_ID
    assert request_count == 2
