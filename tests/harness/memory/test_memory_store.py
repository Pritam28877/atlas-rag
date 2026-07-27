"""Bounded provenance memory behavior."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.services.harness.memory import (
    BoundedMemoryStore,
    MemoryFact,
    MemoryLimits,
    MemoryNamespace,
    MemoryQuery,
    MemoryStoreError,
    MemoryVerification,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
TENANT = "ten_" + "1" * 32
WORKSPACE = "wsp_" + "2" * 32
PRINCIPAL = "prn_" + "3" * 32


def _namespace(principal: str = PRINCIPAL) -> MemoryNamespace:
    return MemoryNamespace(
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        principal_id=principal,
    )


def _fact(
    fact_id: str,
    text: str,
    *,
    namespace: MemoryNamespace | None = None,
    verification: MemoryVerification = MemoryVerification.VERIFIED,
    expires_at: datetime = NOW + timedelta(hours=1),
    relevance: int = 1,
) -> MemoryFact:
    provenance = json.dumps(
        {"source": fact_id},
        separators=(",", ":"),
        sort_keys=True,
    )
    return MemoryFact(
        fact_id=fact_id,
        namespace=namespace or _namespace(),
        text=text,
        source_sha256=hashlib.sha256(fact_id.encode()).hexdigest(),
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        provenance_json=provenance,
        verification=verification,
        relevance=relevance,
        created_at=NOW,
        expires_at=expires_at,
    )


def test_memory_is_namespace_locked_and_skips_unverified_facts() -> None:
    store = BoundedMemoryStore()
    store.put(_fact("verified", "safe"), now=NOW)
    store.put(
        _fact("unverified", "ignore", verification=MemoryVerification.UNVERIFIED),
        now=NOW,
    )

    result = store.retrieve(
        _namespace(),
        MemoryQuery(limit=10, max_bytes=1024),
        now=NOW,
    )
    assert tuple(fact.fact_id for fact in result.facts) == ("verified",)
    assert store.get(_namespace("prn_" + "4" * 32), "verified", now=NOW) is None


def test_memory_ttl_and_quota_are_bounded() -> None:
    store = BoundedMemoryStore(MemoryLimits(max_entries=1, max_bytes=10))
    store.put(_fact("first", "12345", relevance=1), now=NOW)
    store.put(_fact("second", "67890", relevance=2), now=NOW)
    assert store.size == 1
    assert store.get(_namespace(), "first", now=NOW) is None
    assert store.get(_namespace(), "second", now=NOW) is not None
    with pytest.raises(MemoryStoreError):
        store.put(_fact("large", "x" * 11), now=NOW)
    with pytest.raises(MemoryStoreError):
        store.put(
            _fact("expired", "gone", expires_at=NOW + timedelta(hours=1)),
            now=NOW + timedelta(hours=2),
        )
