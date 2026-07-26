import asyncio
import os

import pytest

from app.services.harness.runtime import (
    PeerSessionTokenManager,
    StdioParentCredentialReader,
    StdioParentIdentityError,
    StdioSessionAuthenticator,
    UnixPeerCredentialReader,
)


def test_stdio_session_is_bound_to_stable_parent_process_evidence() -> None:
    async def scenario() -> None:
        reader = StdioParentCredentialReader(
            UnixPeerCredentialReader(),
            parent_process_id=os.getpid,
        )
        session = await StdioSessionAuthenticator(
            reader,
            PeerSessionTokenManager(b"k" * 32),
        ).authenticate()

        assert session.principal.issuer == "atlas-local-peer"
        assert len(session.principal.subject_sha256) == 64
        assert len(session.principal.session_binding_sha256) == 64
        assert session.principal.subject_sha256 != (
            session.principal.session_binding_sha256
        )

    asyncio.run(scenario())


def test_parent_pid_change_during_evidence_collection_fails_closed() -> None:
    async def scenario() -> None:
        process_ids = iter((os.getpid(), os.getpid() + 1))
        reader = StdioParentCredentialReader(
            UnixPeerCredentialReader(),
            parent_process_id=lambda: next(process_ids),
        )

        with pytest.raises(
            StdioParentIdentityError,
            match="authentication failed",
        ):
            await reader.read()

    asyncio.run(scenario())


def test_internal_token_failure_is_redacted() -> None:
    async def scenario() -> None:
        reader = StdioParentCredentialReader(
            UnixPeerCredentialReader(),
            parent_process_id=os.getpid,
        )
        manager = PeerSessionTokenManager(
            b"k" * 32,
            entropy=lambda size: b"",
        )
        with pytest.raises(StdioParentIdentityError) as denied:
            await StdioSessionAuthenticator(reader, manager).authenticate()
        assert str(denied.value) == "stdio parent authentication failed"

    asyncio.run(scenario())


@pytest.mark.parametrize("process_id", (0, 2**31))
def test_invalid_parent_pid_fails_without_process_detail(process_id: int) -> None:
    async def scenario() -> None:
        reader = StdioParentCredentialReader(
            UnixPeerCredentialReader(),
            parent_process_id=lambda: process_id,
        )
        with pytest.raises(StdioParentIdentityError) as denied:
            await reader.read()
        assert str(denied.value) == "stdio parent authentication failed"

    asyncio.run(scenario())
