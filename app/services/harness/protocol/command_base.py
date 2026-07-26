"""Shared command bases that separate selectors from server-owned authority."""

from app.services.harness.protocol.base import IdempotencyKey, StrictProtocolModel


class MutatingCommand(StrictProtocolModel):
    """State-changing command with a caller-stable replay key."""

    idempotency_key: IdempotencyKey


class QueryCommand(StrictProtocolModel):
    """Read-only command that cannot carry mutation replay metadata."""
