"""Provider-neutral streaming body contract."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol


class ProviderStreamingBody(Protocol):
    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def __anext__(self) -> bytes: ...

    async def aclose(self) -> None: ...
