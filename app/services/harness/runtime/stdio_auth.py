"""Parent-process-bound authentication for a client-owned stdio daemon."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable

from app.services.harness.runtime.peer_auth import (
    PeerAuthenticationError,
    PeerCredentials,
    PeerSessionTokenManager,
    VerifiedPeerSession,
)
from app.services.harness.runtime.unix_peer import (
    UnixPeerCredentialError,
    UnixPeerCredentialReader,
)


class StdioParentIdentityError(PermissionError):
    """Generic denial when the spawning process cannot be bound safely."""


class StdioParentCredentialReader:
    """Captures stable parent identity without trusting environment payloads."""

    def __init__(
        self,
        process_reader: UnixPeerCredentialReader,
        *,
        parent_process_id: Callable[[], int] = os.getppid,
    ) -> None:
        self._process_reader = process_reader
        self._parent_process_id = parent_process_id

    async def read(self) -> PeerCredentials:
        process_id_before = self._parent_process_id()
        try:
            credentials = await self._process_reader.read_process_owner(
                process_id_before
            )
        except UnixPeerCredentialError as error:
            raise StdioParentIdentityError(
                "stdio parent authentication failed"
            ) from error
        process_id_after = self._parent_process_id()
        if process_id_before != process_id_after:
            raise StdioParentIdentityError(
                "stdio parent authentication failed"
            )
        return credentials


class StdioSessionAuthenticator:
    """Creates the common session contract without exposing a token to stdio."""

    def __init__(
        self,
        credential_reader: StdioParentCredentialReader,
        token_manager: PeerSessionTokenManager,
    ) -> None:
        self._credential_reader = credential_reader
        self._token_manager = token_manager

    async def authenticate(self) -> VerifiedPeerSession:
        credentials = await self._credential_reader.read()
        try:
            token = self._token_manager.issue(credentials)
            return await asyncio.to_thread(
                self._token_manager.verify_and_consume,
                token,
                credentials,
            )
        except (PeerAuthenticationError, RuntimeError) as error:
            raise StdioParentIdentityError(
                "stdio parent authentication failed"
            ) from error
