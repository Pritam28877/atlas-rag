import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import jwt
from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import AuthSettings, ProviderTimeoutSettings


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    tenant_id: UUID


class OidcVerifier:
    """Validate OIDC access tokens against a bounded, asynchronously refreshed JWKS."""

    def __init__(
        self,
        settings: AuthSettings,
        timeouts: ProviderTimeoutSettings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.issuer or not settings.audience or not settings.jwks_url:
            raise ValueError("OIDC authentication is not configured")
        self._settings = settings
        self._jwks_url = settings.jwks_url
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=timeouts.connect_seconds,
                read=timeouts.request_seconds,
                write=timeouts.request_seconds,
                pool=timeouts.connect_seconds,
            ),
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._keys: dict[str, Any] = {}
        self._keys_expire_at = 0.0
        self._last_unknown_key_refresh_at = float("-inf")
        self._unknown_key_refresh_cooldown_seconds = min(
            60, settings.jwks_cache_ttl_seconds
        )
        self._refresh_lock = asyncio.Lock()

    async def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
            key_id = header.get("kid")
            algorithm = header.get("alg")
            if not isinstance(key_id, str) or algorithm != "RS256":
                raise jwt.InvalidTokenError("unsupported token header")
            signing_key = await self._get_signing_key(key_id)
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self._settings.audience,
                issuer=self._settings.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
            tenant_value = claims.get(self._settings.tenant_claim)
            subject = claims.get("sub")
            if not isinstance(subject, str) or not subject:
                raise jwt.InvalidTokenError("subject claim is invalid")
            if not isinstance(tenant_value, str):
                raise jwt.InvalidTokenError("tenant claim is invalid")
            return Principal(subject=subject, tenant_id=UUID(tenant_value))
        except (jwt.PyJWTError, ValueError, httpx.HTTPError) as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from error

    async def _get_signing_key(self, key_id: str) -> Any:
        now = time.monotonic()
        if now < self._keys_expire_at:
            signing_key = self._keys.get(key_id)
            if signing_key is not None:
                return signing_key
        async with self._refresh_lock:
            now = time.monotonic()
            if now >= self._keys_expire_at:
                await self._refresh_keys(now)
            elif key_id not in self._keys and self._can_refresh_unknown_key(now):
                await self._refresh_keys(now)
                self._last_unknown_key_refresh_at = now
            signing_key = self._keys.get(key_id)
            if signing_key is None:
                raise jwt.InvalidTokenError("signing key is unavailable")
            return signing_key

    def _can_refresh_unknown_key(self, now: float) -> bool:
        elapsed = now - self._last_unknown_key_refresh_at
        return elapsed >= self._unknown_key_refresh_cooldown_seconds

    async def _refresh_keys(self, now: float) -> None:
        maximum_jwks_bytes = 1024 * 1024
        content = bytearray()
        async with self._client.stream("GET", self._jwks_url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > maximum_jwks_bytes:
                    raise jwt.InvalidTokenError("JWKS response exceeds size limit")
        payload = json.loads(content)
        key_set = jwt.PyJWKSet.from_dict(payload)
        keys = {key.key_id: key.key for key in key_set.keys if key.key_id}
        if not keys:
            raise jwt.InvalidTokenError("JWKS contains no usable keys")
        self._keys = keys
        self._keys_expire_at = now + self._settings.jwks_cache_ttl_seconds

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


bearer_scheme = HTTPBearer(auto_error=False)


async def require_principal(request: Request) -> Principal:
    credentials: HTTPAuthorizationCredentials | None = await bearer_scheme(request)
    verifier: OidcVerifier | None = getattr(request.app.state, "auth", None)
    if credentials is None or verifier is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bearer authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if len(credentials.credentials) > 16_384:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await verifier.verify(credentials.credentials)
