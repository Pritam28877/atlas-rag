import asyncio
import os
import socket
from pathlib import Path

import pytest

from app.services.harness.runtime import (
    UnixPeerCredentialError,
    UnixPeerCredentialReader,
)

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "SO_PEERCRED") or not Path("/proc/self").exists(),
    reason="Linux SO_PEERCRED and procfs are required",
)


def test_unix_peer_credentials_are_bound_to_kernel_process_evidence() -> None:
    async def scenario() -> None:
        server_socket, client_socket = socket.socketpair()
        try:
            credentials = await UnixPeerCredentialReader().read(server_socket)
        finally:
            server_socket.close()
            client_socket.close()

        assert credentials.process_id == os.getpid()
        assert credentials.user_id == os.getuid()
        assert credentials.group_id == os.getgid()
        assert credentials.process_start_ticks > 0
        assert len(credentials.executable_sha256) == 64
        assert len(credentials.binding_sha256()) == 64

    asyncio.run(scenario())


def test_non_unix_socket_fails_before_peer_identity_lookup() -> None:
    async def scenario() -> None:
        network_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(
                UnixPeerCredentialError,
                match="authentication failed",
            ):
                await UnixPeerCredentialReader().read(network_socket)
        finally:
            network_socket.close()

    asyncio.run(scenario())


def test_executable_hashing_has_a_reviewed_hard_size_limit() -> None:
    async def scenario() -> None:
        server_socket, client_socket = socket.socketpair()
        try:
            with pytest.raises(
                UnixPeerCredentialError,
                match="authentication failed",
            ):
                await UnixPeerCredentialReader(
                    maximum_executable_bytes=1
                ).read(server_socket)
        finally:
            server_socket.close()
            client_socket.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("proc_root", "maximum_executable_bytes"),
    ((Path("proc"), 1024), (Path("/proc"), 0), (Path("/proc"), 1024**3 + 1)),
)
def test_peer_reader_configuration_is_bounded(
    proc_root: Path,
    maximum_executable_bytes: int,
) -> None:
    with pytest.raises(ValueError):
        UnixPeerCredentialReader(
            proc_root=proc_root,
            maximum_executable_bytes=maximum_executable_bytes,
        )
