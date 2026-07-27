"""Bounded one-shot claims that prevent in-process permit replay."""

import asyncio
from enum import StrEnum


class ToolDispatchAdmissionErrorCode(StrEnum):
    CAPACITY = "capacity"
    DUPLICATE = "duplicate"


class ToolDispatchAdmissionError(RuntimeError):
    def __init__(self, code: ToolDispatchAdmissionErrorCode) -> None:
        super().__init__("tool dispatch admission failed")
        self.code = code


class OperationClaimGuard:
    def __init__(self, maximum_claimed_operations: int) -> None:
        if not 1 <= maximum_claimed_operations <= 65_536:
            raise ValueError("tool dispatch claim capacity is outside bounds")
        self._maximum_claimed_operations = maximum_claimed_operations
        self._claimed_operation_ids: set[str] = set()
        self._lock = asyncio.Lock()

    async def claim(self, operation_id: str) -> None:
        async with self._lock:
            if operation_id in self._claimed_operation_ids:
                raise ToolDispatchAdmissionError(
                    ToolDispatchAdmissionErrorCode.DUPLICATE
                )
            if (
                len(self._claimed_operation_ids)
                >= self._maximum_claimed_operations
            ):
                raise ToolDispatchAdmissionError(
                    ToolDispatchAdmissionErrorCode.CAPACITY
                )
            self._claimed_operation_ids.add(operation_id)
