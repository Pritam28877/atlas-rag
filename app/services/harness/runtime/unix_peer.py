"""Kernel-backed Unix peer identity with PID-reuse-resistant evidence."""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import stat
import struct
from pathlib import Path

from app.services.harness.runtime.peer_auth import PeerCredentials

_PEER_CREDENTIALS = struct.Struct("3i")


class UnixPeerCredentialError(PermissionError):
    """Generic local peer failure that does not disclose process details."""


class UnixPeerCredentialReader:
    """Resolves SO_PEERCRED and stable bounded process evidence on Linux."""

    def __init__(
        self,
        *,
        proc_root: Path = Path("/proc"),
        maximum_executable_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        if not proc_root.is_absolute():
            raise ValueError("proc_root must be absolute")
        if not 1 <= maximum_executable_bytes <= 1024 * 1024 * 1024:
            raise ValueError(
                "maximum_executable_bytes must be between 1 byte and 1 GiB"
            )
        self._proc_root = proc_root
        self._maximum_executable_bytes = maximum_executable_bytes

    async def read(self, peer_socket: socket.socket) -> PeerCredentials:
        if peer_socket.family != socket.AF_UNIX:
            raise UnixPeerCredentialError("local peer authentication failed")
        try:
            raw_credentials = peer_socket.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                _PEER_CREDENTIALS.size,
            )
            process_id, user_id, group_id = _PEER_CREDENTIALS.unpack(
                raw_credentials
            )
            start_ticks, executable_sha256 = await asyncio.to_thread(
                self._read_stable_process_evidence,
                process_id,
            )
            return PeerCredentials(
                user_id=user_id,
                group_id=group_id,
                process_id=process_id,
                process_start_ticks=start_ticks,
                executable_sha256=executable_sha256,
            )
        except (OSError, ValueError) as error:
            raise UnixPeerCredentialError(
                "local peer authentication failed"
            ) from error

    async def read_process_owner(self, process_id: int) -> PeerCredentials:
        """Reads procfs owner and stable evidence for a known parent process."""

        try:
            return await asyncio.to_thread(
                self._read_process_owner,
                process_id,
            )
        except (OSError, ValueError) as error:
            raise UnixPeerCredentialError(
                "local peer authentication failed"
            ) from error

    def _read_process_owner(self, process_id: int) -> PeerCredentials:
        if not 1 <= process_id <= 2**31 - 1:
            raise UnixPeerCredentialError("local peer authentication failed")
        process_directory = self._proc_root / str(process_id)
        process_status = process_directory.stat()
        start_ticks, executable_sha256 = self._read_stable_process_evidence(
            process_id
        )
        return PeerCredentials(
            user_id=process_status.st_uid,
            group_id=process_status.st_gid,
            process_id=process_id,
            process_start_ticks=start_ticks,
            executable_sha256=executable_sha256,
        )

    def _read_stable_process_evidence(self, process_id: int) -> tuple[int, str]:
        process_directory = self._proc_root / str(process_id)
        start_ticks_before = self._read_start_ticks(process_directory / "stat")
        executable_sha256 = self._hash_executable(process_directory / "exe")
        start_ticks_after = self._read_start_ticks(process_directory / "stat")
        if start_ticks_before != start_ticks_after:
            raise UnixPeerCredentialError("local peer authentication failed")
        return start_ticks_before, executable_sha256

    @staticmethod
    def _read_start_ticks(stat_path: Path) -> int:
        with stat_path.open("rb") as stat_file:
            content = stat_file.read(4_097)
        if len(content) > 4_096:
            raise UnixPeerCredentialError("local peer authentication failed")
        command_end = content.rfind(b")")
        if command_end < 0:
            raise UnixPeerCredentialError("local peer authentication failed")
        remaining_fields = content[command_end + 2 :].split()
        try:
            start_ticks = int(remaining_fields[19])
        except (IndexError, ValueError) as error:
            raise UnixPeerCredentialError(
                "local peer authentication failed"
            ) from error
        if start_ticks < 1:
            raise UnixPeerCredentialError("local peer authentication failed")
        return start_ticks

    def _hash_executable(self, executable_path: Path) -> str:
        digest = hashlib.sha256()
        with executable_path.open("rb", buffering=0) as executable:
            before = os.fstat(executable.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size > self._maximum_executable_bytes
            ):
                raise UnixPeerCredentialError(
                    "local peer authentication failed"
                )
            remaining_bytes = before.st_size
            while remaining_bytes:
                chunk = executable.read(min(1024 * 1024, remaining_bytes))
                if not chunk:
                    raise UnixPeerCredentialError(
                        "local peer authentication failed"
                    )
                digest.update(chunk)
                remaining_bytes -= len(chunk)
            after = os.fstat(executable.fileno())
        stable_fields_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        stable_fields_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if stable_fields_before != stable_fields_after:
            raise UnixPeerCredentialError("local peer authentication failed")
        return digest.hexdigest()
