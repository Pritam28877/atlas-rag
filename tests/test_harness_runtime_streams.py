import asyncio

import pytest

from app.services.harness.runtime import LocalStreamRunner


class Connection:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.closed = False

    async def receive(self, chunk: bytes, sender: object) -> None:
        self.chunks.append(chunk)

    async def close(self) -> None:
        self.closed = True


class Writer:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.drain_calls = 0
        self.closed = False
        self.waited = False

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        self.drain_calls += 1

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


class SendingConnection(Connection):
    async def receive(self, chunk: bytes, sender: object) -> None:
        self.chunks.append(chunk)
        await sender(b"response")  # type: ignore[operator]


def reader_with(*chunks: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()
    return reader


def test_stream_runner_reads_bounded_chunks_and_drains_each_response() -> None:
    async def scenario() -> None:
        connection = SendingConnection()
        writer = Writer()
        runner = LocalStreamRunner(read_chunk_bytes=3)

        await runner.serve(reader_with(b"abcdef"), writer, connection)  # type: ignore[arg-type]

        assert connection.chunks == [b"abc", b"def"]
        assert writer.frames == [b"response", b"response"]
        assert writer.drain_calls == 2
        assert connection.closed
        assert writer.closed
        assert writer.waited

    asyncio.run(scenario())


def test_cancellation_still_closes_connection_and_writer() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        connection = Connection()
        writer = Writer()
        runner = LocalStreamRunner(read_chunk_bytes=64)
        task = asyncio.create_task(
            runner.serve(reader, writer, connection)  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert connection.closed
        assert writer.closed
        assert writer.waited

    asyncio.run(scenario())


def test_processing_failure_is_propagated_after_cleanup() -> None:
    class FailingConnection(Connection):
        async def receive(self, chunk: bytes, sender: object) -> None:
            raise RuntimeError("processing failed")

    async def scenario() -> None:
        connection = FailingConnection()
        writer = Writer()
        runner = LocalStreamRunner(read_chunk_bytes=64)

        with pytest.raises(RuntimeError, match="processing failed"):
            await runner.serve(
                reader_with(b"request"),
                writer,  # type: ignore[arg-type]
                connection,  # type: ignore[arg-type]
            )

        assert connection.closed
        assert writer.closed
        assert writer.waited

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("read_chunk_bytes", "shutdown_timeout_seconds"),
    ((0, 1), (4 * 1024 * 1024 + 5, 1), (1024, 0), (1024, 31)),
)
def test_stream_limits_are_hard_bounded(
    read_chunk_bytes: int,
    shutdown_timeout_seconds: float,
) -> None:
    with pytest.raises(ValueError):
        LocalStreamRunner(
            read_chunk_bytes=read_chunk_bytes,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
        )
