"""Incremental bounded framing for provider SSE data records."""

from __future__ import annotations

from enum import StrEnum

MAXIMUM_SSE_LINE_BYTES = 64 * 1024
MAXIMUM_SSE_RECORD_BYTES = 64 * 1024
MAXIMUM_SSE_RECORDS = 4_096
MAXIMUM_SSE_STREAM_BYTES = 16 * 1024 * 1024


class SseFramingErrorCode(StrEnum):
    LINE_SIZE = "line_size"
    MALFORMED = "malformed"
    RECORD_LIMIT = "record_limit"
    RECORD_SIZE = "record_size"
    STREAM_SIZE = "stream_size"
    TRUNCATED = "truncated"


class SseFramingError(ValueError):
    def __init__(self, code: SseFramingErrorCode) -> None:
        super().__init__("provider SSE framing failed")
        self.code = code


class BoundedSseFramer:
    def __init__(self) -> None:
        self._buffer = bytearray()
        self._data_lines: list[bytes] = []
        self._data_bytes = 0
        self._record_count = 0
        self._stream_bytes = 0

    def feed(self, chunk: bytes) -> tuple[bytes, ...]:
        if not isinstance(chunk, bytes):
            raise TypeError("SSE chunks must be bytes")
        if not chunk:
            return ()
        self._stream_bytes += len(chunk)
        if self._stream_bytes > MAXIMUM_SSE_STREAM_BYTES:
            raise SseFramingError(SseFramingErrorCode.STREAM_SIZE)
        self._buffer.extend(chunk)
        if len(self._buffer) > MAXIMUM_SSE_LINE_BYTES and b"\n" not in (
            self._buffer
        ):
            raise SseFramingError(SseFramingErrorCode.LINE_SIZE)
        records: list[bytes] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            records.extend(self._line(line))
        return tuple(records)

    def finish(self) -> None:
        if self._buffer or self._data_lines:
            raise SseFramingError(SseFramingErrorCode.TRUNCATED)

    def _line(self, line: bytes) -> tuple[bytes, ...]:
        if len(line) > MAXIMUM_SSE_LINE_BYTES:
            raise SseFramingError(SseFramingErrorCode.LINE_SIZE)
        if not line:
            return self._complete_record()
        if line.startswith(b":"):
            return ()
        if not line.startswith(b"data:"):
            raise SseFramingError(SseFramingErrorCode.MALFORMED)
        data = line[5:]
        if data.startswith(b" "):
            data = data[1:]
        added_bytes = len(data) + int(bool(self._data_lines))
        self._data_bytes += added_bytes
        if self._data_bytes > MAXIMUM_SSE_RECORD_BYTES:
            raise SseFramingError(SseFramingErrorCode.RECORD_SIZE)
        self._data_lines.append(data)
        return ()

    def _complete_record(self) -> tuple[bytes, ...]:
        if not self._data_lines:
            return ()
        self._record_count += 1
        if self._record_count > MAXIMUM_SSE_RECORDS:
            raise SseFramingError(SseFramingErrorCode.RECORD_LIMIT)
        record = b"\n".join(self._data_lines)
        self._data_lines.clear()
        self._data_bytes = 0
        if not record:
            raise SseFramingError(SseFramingErrorCode.MALFORMED)
        return (record,)
