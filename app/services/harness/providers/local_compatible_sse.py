"""Compatibility names for the provider-neutral SSE framer."""

from app.services.harness.providers.sse_framer import (
    MAXIMUM_SSE_LINE_BYTES as MAXIMUM_LOCAL_SSE_LINE_BYTES,
)
from app.services.harness.providers.sse_framer import (
    MAXIMUM_SSE_RECORD_BYTES as MAXIMUM_LOCAL_SSE_RECORD_BYTES,
)
from app.services.harness.providers.sse_framer import (
    MAXIMUM_SSE_RECORDS as MAXIMUM_LOCAL_SSE_RECORDS,
)
from app.services.harness.providers.sse_framer import (
    MAXIMUM_SSE_STREAM_BYTES as MAXIMUM_LOCAL_SSE_STREAM_BYTES,
)
from app.services.harness.providers.sse_framer import (
    BoundedSseFramer,
)
from app.services.harness.providers.sse_framer import (
    SseFramingError as LocalSseError,
)
from app.services.harness.providers.sse_framer import (
    SseFramingErrorCode as LocalSseErrorCode,
)

__all__ = (
    "MAXIMUM_LOCAL_SSE_LINE_BYTES",
    "MAXIMUM_LOCAL_SSE_RECORD_BYTES",
    "MAXIMUM_LOCAL_SSE_RECORDS",
    "MAXIMUM_LOCAL_SSE_STREAM_BYTES",
    "BoundedSseFramer",
    "LocalSseError",
    "LocalSseErrorCode",
)
