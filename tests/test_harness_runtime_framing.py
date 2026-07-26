import asyncio
import random
import time
from pathlib import Path

import pytest

from app.services.harness.runtime import (
    AdmissionError,
    AdmissionErrorCode,
    FrameDecoder,
    FrameError,
    FrameErrorCode,
    RequestAdmission,
    decode_command,
    encode_frame,
)

ROOT = Path(__file__).resolve().parents[1]
COMMAND_FIXTURE = (
    ROOT / "tests/fixtures/harness/protocol/reader-v1.2-command.json"
)


def test_fragmented_frame_is_reassembled_without_retaining_bytes() -> None:
    payload = COMMAND_FIXTURE.read_bytes()
    encoded = encode_frame(payload, maximum_frame_bytes=4096)
    decoder = FrameDecoder(maximum_frame_bytes=4096)
    emitted: list[bytes] = []

    for byte in encoded:
        emitted.extend(decoder.feed(bytes((byte,))))

    assert emitted == [payload]
    assert decoder.buffered_bytes == 0


def test_multiple_frames_are_decoded_in_order() -> None:
    first = b'{"first":true}'
    second = b'{"second":true}'
    wire_bytes = encode_frame(
        first,
        maximum_frame_bytes=256,
    ) + encode_frame(second, maximum_frame_bytes=256)
    decoder = FrameDecoder(maximum_frame_bytes=256)

    assert decoder.feed(wire_bytes) == (first, second)
    assert decoder.buffered_bytes == 0


def test_malformed_lengths_fail_fatally_and_clear_buffer() -> None:
    decoder = FrameDecoder(maximum_frame_bytes=64)

    with pytest.raises(FrameError) as empty_error:
        decoder.feed((0).to_bytes(4, byteorder="big"))
    assert empty_error.value.code is FrameErrorCode.EMPTY_FRAME
    assert empty_error.value.fatal is True
    assert decoder.buffered_bytes == 0

    with pytest.raises(FrameError) as oversized_error:
        decoder.feed((65).to_bytes(4, byteorder="big"))
    assert oversized_error.value.code is FrameErrorCode.FRAME_TOO_LARGE
    assert decoder.buffered_bytes == 0


def test_decoder_rejects_chunks_beyond_its_fixed_memory_bound() -> None:
    decoder = FrameDecoder(maximum_frame_bytes=64)

    with pytest.raises(FrameError) as error:
        decoder.feed(b"x" * 69)

    assert error.value.code is FrameErrorCode.BUFFER_LIMIT
    assert decoder.buffered_bytes == 0


def test_deterministic_fragment_fuzz_never_changes_payload() -> None:
    random_source = random.Random(20260726)
    payload = COMMAND_FIXTURE.read_bytes()
    encoded = encode_frame(payload, maximum_frame_bytes=4096)

    for _ in range(200):
        decoder = FrameDecoder(maximum_frame_bytes=4096)
        emitted: list[bytes] = []
        offset = 0
        while offset < len(encoded):
            chunk_size = random_source.randint(1, 31)
            emitted.extend(decoder.feed(encoded[offset : offset + chunk_size]))
            offset += chunk_size
        assert emitted == [payload]
        assert decoder.buffered_bytes == 0


def test_deterministic_malformed_fuzz_stays_inside_buffer_bound() -> None:
    random_source = random.Random(20260727)

    for _ in range(500):
        decoder = FrameDecoder(maximum_frame_bytes=64)
        chunk = random_source.randbytes(random_source.randint(0, 96))
        try:
            decoder.feed(chunk)
        except FrameError:
            pass
        assert decoder.buffered_bytes <= 68


def test_command_admission_rejects_malformed_json_without_details() -> None:
    with pytest.raises(AdmissionError) as error:
        decode_command(b'{"schema_version":"1.2","command":')

    assert error.value.code is AdmissionErrorCode.MALFORMED_COMMAND
    assert str(error.value) == "request is not a valid command envelope"


def test_valid_command_is_decoded_after_framing() -> None:
    command = decode_command(COMMAND_FIXTURE.read_bytes())

    assert command.schema_version == "1.2"
    assert command.command.kind == "event.subscribe"


def test_admission_cancellation_releases_raced_permit() -> None:
    async def exercise() -> None:
        admission = RequestAdmission(
            maximum_concurrency=1,
            maximum_request_seconds=1,
        )
        first = await admission.acquire(
            deadline_monotonic=time.monotonic() + 0.5,
            cancellation_event=asyncio.Event(),
        )
        cancellation_event = asyncio.Event()
        waiter = asyncio.create_task(
            admission.acquire(
                deadline_monotonic=time.monotonic() + 0.5,
                cancellation_event=cancellation_event,
            )
        )
        await asyncio.sleep(0)
        cancellation_event.set()
        with pytest.raises(AdmissionError) as error:
            await waiter
        assert error.value.code is AdmissionErrorCode.CANCELLED
        first.release()

        replacement = await admission.acquire(
            deadline_monotonic=time.monotonic() + 0.5,
            cancellation_event=asyncio.Event(),
        )
        replacement.release()

    asyncio.run(exercise())


def test_admission_deadlines_and_context_release_are_bounded() -> None:
    async def exercise() -> None:
        admission = RequestAdmission(
            maximum_concurrency=1,
            maximum_request_seconds=0.5,
        )
        with pytest.raises(AdmissionError) as oversized:
            await admission.acquire(
                deadline_monotonic=time.monotonic() + 1,
                cancellation_event=asyncio.Event(),
            )
        assert oversized.value.code is AdmissionErrorCode.DEADLINE_TOO_LARGE

        lease = await admission.acquire(
            deadline_monotonic=time.monotonic() + 0.4,
            cancellation_event=asyncio.Event(),
        )
        with pytest.raises(AdmissionError) as expired:
            await admission.acquire(
                deadline_monotonic=time.monotonic() + 0.01,
                cancellation_event=asyncio.Event(),
            )
        assert expired.value.code is AdmissionErrorCode.DEADLINE_EXCEEDED
        lease.release()

        async with await admission.acquire(
            deadline_monotonic=time.monotonic() + 0.4,
            cancellation_event=asyncio.Event(),
        ):
            pass

    asyncio.run(exercise())
