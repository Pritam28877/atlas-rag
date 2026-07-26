"""Bounded length-prefixed framing for local harness transports."""

from __future__ import annotations

from enum import StrEnum

HEADER_BYTES = 4


class FrameErrorCode(StrEnum):
    EMPTY_FRAME = "empty_frame"
    FRAME_TOO_LARGE = "frame_too_large"
    BUFFER_LIMIT = "buffer_limit"


class FrameError(ValueError):
    """Fatal framing violation with a stable client-safe code."""

    def __init__(self, code: FrameErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.fatal = True


def encode_frame(payload: bytes, *, maximum_frame_bytes: int) -> bytes:
    payload_size = len(payload)
    if payload_size == 0:
        raise FrameError(FrameErrorCode.EMPTY_FRAME, "frame payload cannot be empty")
    if payload_size > maximum_frame_bytes:
        raise FrameError(
            FrameErrorCode.FRAME_TOO_LARGE,
            "frame payload exceeds configured maximum",
        )
    return payload_size.to_bytes(HEADER_BYTES, byteorder="big") + payload


class FrameDecoder:
    """Incremental decoder with memory bounded to one configured frame."""

    def __init__(self, *, maximum_frame_bytes: int) -> None:
        if not 1 <= maximum_frame_bytes <= 4 * 1024 * 1024:
            raise ValueError("maximum_frame_bytes must be between 1 and 4 MiB")
        self._maximum_frame_bytes = maximum_frame_bytes
        self._maximum_buffer_bytes = maximum_frame_bytes + HEADER_BYTES
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> tuple[bytes, ...]:
        if len(chunk) > self._maximum_buffer_bytes:
            self._fail(
                FrameErrorCode.BUFFER_LIMIT,
                "transport chunk exceeds decoder buffer limit",
            )
        self._buffer.extend(chunk)
        if len(self._buffer) > self._maximum_buffer_bytes:
            self._fail(
                FrameErrorCode.BUFFER_LIMIT,
                "fragmented frame exceeds decoder buffer limit",
            )

        frames: list[bytes] = []
        consumed_bytes = 0
        while len(self._buffer) - consumed_bytes >= HEADER_BYTES:
            header_end = consumed_bytes + HEADER_BYTES
            payload_size = int.from_bytes(
                self._buffer[consumed_bytes:header_end],
                byteorder="big",
            )
            if payload_size == 0:
                self._fail(
                    FrameErrorCode.EMPTY_FRAME,
                    "frame header declares an empty payload",
                )
            if payload_size > self._maximum_frame_bytes:
                self._fail(
                    FrameErrorCode.FRAME_TOO_LARGE,
                    "frame header exceeds configured maximum",
                )
            frame_end = header_end + payload_size
            if frame_end > len(self._buffer):
                break
            frames.append(bytes(self._buffer[header_end:frame_end]))
            consumed_bytes = frame_end

        if consumed_bytes:
            del self._buffer[:consumed_bytes]
        return tuple(frames)

    def reset(self) -> None:
        self._buffer.clear()

    def _fail(self, code: FrameErrorCode, message: str) -> None:
        self._buffer.clear()
        raise FrameError(code, message)
