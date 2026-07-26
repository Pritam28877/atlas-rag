"""Bounded asyncio stream ownership shared by local transports."""

from __future__ import annotations

import asyncio
from typing import Protocol

from app.services.harness.runtime.connection import (
    FrameSender,
    LocalConnection,
)


class AsyncStreamWriter(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


class DrainingFrameSender:
    """Writes one frame and waits for transport backpressure."""

    def __init__(self, writer: AsyncStreamWriter) -> None:
        self._writer = writer

    async def __call__(self, frame: bytes) -> None:
        self._writer.write(frame)
        await self._writer.drain()


class LocalStreamRunner:
    """Reads bounded chunks and deterministically releases stream ownership."""

    def __init__(
        self,
        *,
        read_chunk_bytes: int,
        shutdown_timeout_seconds: float = 5,
    ) -> None:
        if not 1 <= read_chunk_bytes <= 4 * 1024 * 1024 + 4:
            raise ValueError("read_chunk_bytes must be between 1 byte and 4 MiB + 4")
        if not 0.001 <= shutdown_timeout_seconds <= 30:
            raise ValueError(
                "shutdown_timeout_seconds must be between 1 ms and 30 seconds"
            )
        self._read_chunk_bytes = read_chunk_bytes
        self._shutdown_timeout_seconds = shutdown_timeout_seconds

    async def serve(
        self,
        reader: asyncio.StreamReader,
        writer: AsyncStreamWriter,
        connection: LocalConnection,
    ) -> None:
        sender: FrameSender = DrainingFrameSender(writer)
        try:
            while chunk := await reader.read(self._read_chunk_bytes):
                await connection.receive(chunk, sender)
        finally:
            await connection.close()
            writer.close()
            async with asyncio.timeout(self._shutdown_timeout_seconds):
                await writer.wait_closed()
