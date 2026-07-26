"""Exact bounded Botocore loaders for network-backed AWS identities."""

from __future__ import annotations

from collections.abc import Callable
from typing import Never, Protocol, cast

# Botocore does not publish PEP 561 typing metadata.
from botocore import UNSIGNED  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.credentials import (  # type: ignore[import-untyped]
    AssumeRoleWithWebIdentityProvider,
    InstanceMetadataProvider,
)
from botocore.session import Session  # type: ignore[import-untyped]
from botocore.utils import (  # type: ignore[import-untyped]
    InstanceMetadataFetcher,
)

from app.services.harness.providers.bedrock_botocore_sources import (
    read_private_credential_file,
)
from app.services.harness.providers.bedrock_credentials import (
    BedrockCredentialMaterial,
    BedrockCredentialResolutionError,
    BedrockCredentialResolutionErrorCode,
)
from app.services.harness.providers.bedrock_identity import (
    BedrockCredentialSourceKind,
    BedrockIdentityReference,
)
from app.services.harness.providers.bedrock_policy import (
    AuthorizedBedrockRoute,
)

MAXIMUM_WEB_IDENTITY_TOKEN_BYTES = 64 * 1024
INSTANCE_METADATA_URL = "http://169.254.169.254/"


class _FrozenCredentials(Protocol):
    access_key: str
    secret_key: str
    token: str | None


class _Credentials(Protocol):
    def get_frozen_credentials(self) -> _FrozenCredentials: ...


NetworkSourceLoader = Callable[
    [BedrockIdentityReference, AuthorizedBedrockRoute],
    BedrockCredentialMaterial,
]


class BotocoreNetworkCredentialBackend:
    def __init__(
        self,
        route: AuthorizedBedrockRoute,
        *,
        web_identity_loader: NetworkSourceLoader | None = None,
        instance_metadata_loader: NetworkSourceLoader | None = None,
    ) -> None:
        self._route = route
        self._web_identity_loader = web_identity_loader or _load_web_identity
        self._instance_metadata_loader = (
            instance_metadata_loader or _load_instance_metadata
        )

    def load(
        self,
        identity: BedrockIdentityReference,
    ) -> BedrockCredentialMaterial:
        if (
            identity.identity_reference_id != self._route.identity_reference_id
            or identity.credential_handle != self._route.credential_handle
        ):
            _reject()
        if identity.source is BedrockCredentialSourceKind.WEB_IDENTITY:
            return self._web_identity_loader(identity, self._route)
        if identity.source is BedrockCredentialSourceKind.INSTANCE_METADATA:
            return self._instance_metadata_loader(identity, self._route)
        _reject()


def _load_web_identity(
    identity: BedrockIdentityReference,
    route: AuthorizedBedrockRoute,
) -> BedrockCredentialMaterial:
    token_file = identity.web_identity_token_file
    role_arn = identity.role_arn
    session_name = identity.role_session_name
    if token_file is None or role_arn is None or session_name is None:
        _reject()
    token = read_private_credential_file(
        token_file,
        maximum_bytes=MAXIMUM_WEB_IDENTITY_TOKEN_BYTES,
    ).decode("utf-8")
    if not token.strip() or "\x00" in token:
        _reject()
    profile_name = "atlas-web-identity"

    def load_config() -> dict[str, object]:
        return {
            "profiles": {
                profile_name: {
                    "role_arn": role_arn,
                    "role_session_name": session_name,
                    "web_identity_token_file": str(token_file),
                }
            }
        }

    def token_loader_factory(_path: str) -> Callable[[], str]:
        def load_token() -> str:
            return token

        return load_token

    session = Session()

    def client_creator(
        service_name: str,
        **_kwargs: object,
    ) -> object:
        if service_name != "sts":
            _reject()
        return session.create_client(
            "sts",
            region_name=route.region,
            endpoint_url=_sts_endpoint(route.region),
            config=Config(
                connect_timeout=1,
                read_timeout=5,
                retries={"max_attempts": 0},
                signature_version=UNSIGNED,
            ),
        )

    provider = AssumeRoleWithWebIdentityProvider(
        load_config=load_config,
        client_creator=client_creator,
        profile_name=profile_name,
        disable_env_vars=True,
        token_loader_cls=token_loader_factory,
    )
    return _material(provider.load())


def _load_instance_metadata(
    _identity: BedrockIdentityReference,
    _route: AuthorizedBedrockRoute,
) -> BedrockCredentialMaterial:
    fetcher = InstanceMetadataFetcher(
        timeout=1,
        num_attempts=1,
        base_url=INSTANCE_METADATA_URL,
        env={"AWS_EC2_METADATA_DISABLED": "false"},
    )
    provider = InstanceMetadataProvider(iam_role_fetcher=fetcher)
    return _material(provider.load())


def _material(credentials: object) -> BedrockCredentialMaterial:
    if credentials is None:
        _reject()
    typed = cast(_Credentials, credentials)
    try:
        frozen = typed.get_frozen_credentials()
    except Exception:
        _reject()
    token = bytearray(frozen.token.encode()) if frozen.token is not None else None
    return BedrockCredentialMaterial(
        bytearray(frozen.access_key.encode()),
        bytearray(frozen.secret_key.encode()),
        token,
        expires_at=None,
    )


def _sts_endpoint(region: str) -> str:
    suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    return f"https://sts.{region}.{suffix}"


def _reject() -> Never:
    raise BedrockCredentialResolutionError(BedrockCredentialResolutionErrorCode.LOAD)
