"""Bounded, provenance-carrying memory facts with namespace isolation."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    PrincipalId,
    Sha256,
    StrictProtocolModel,
    TenantId,
    UtcTimestamp,
    WorkspaceId,
)
from app.services.harness.protocol.conversation import SourceId

MEMORY_DEFAULT_MAX_ENTRIES = 10_000
MEMORY_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
MEMORY_MAX_FACT_BYTES = 64 * 1024
MEMORY_MAX_RETRIEVAL_COUNT = 256


class MemoryVerification(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    REVOKED = "revoked"


class MemoryNamespace(StrictProtocolModel):
    tenant_id: TenantId
    workspace_id: WorkspaceId
    principal_id: PrincipalId


class MemoryFact(StrictProtocolModel):
    fact_id: SourceId
    namespace: MemoryNamespace
    text: str = Field(min_length=1, max_length=MEMORY_MAX_FACT_BYTES)
    source_sha256: Sha256
    content_sha256: Sha256
    provenance_json: str = Field(min_length=2, max_length=16 * 1024)
    verification: MemoryVerification
    relevance: int = Field(default=0, ge=0, le=1_000_000)
    created_at: UtcTimestamp
    expires_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_fact(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("memory fact expiry must follow creation")
        encoded = self.text.encode("utf-8")
        if len(encoded) > MEMORY_MAX_FACT_BYTES:
            raise ValueError("memory fact exceeds byte limit")
        if hashlib.sha256(encoded).hexdigest() != self.content_sha256:
            raise ValueError("memory content hash is invalid")
        try:
            value = json.loads(self.provenance_json)
            canonical = json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("memory provenance must be valid JSON") from error
        if not isinstance(value, dict) or canonical != self.provenance_json:
            raise ValueError("memory provenance must be canonical JSON")
        return self


class MemoryLimits(StrictProtocolModel):
    max_entries: int = Field(
        default=MEMORY_DEFAULT_MAX_ENTRIES,
        ge=1,
        le=MEMORY_DEFAULT_MAX_ENTRIES,
    )
    max_bytes: int = Field(
        default=MEMORY_DEFAULT_MAX_BYTES,
        ge=1,
        le=MEMORY_DEFAULT_MAX_BYTES,
    )


class MemoryQuery(StrictProtocolModel):
    limit: int = Field(default=32, ge=1, le=MEMORY_MAX_RETRIEVAL_COUNT)
    max_bytes: int = Field(default=256 * 1024, ge=1, le=MEMORY_DEFAULT_MAX_BYTES)
    deadline_ms: int = Field(default=100, ge=1, le=10_000)


class MemoryRetrieval(StrictProtocolModel):
    facts: tuple[MemoryFact, ...] = Field(max_length=MEMORY_MAX_RETRIEVAL_COUNT)
    total_bytes: int = Field(ge=0, le=MEMORY_DEFAULT_MAX_BYTES)
    timed_out: bool


class MemoryStoreError(ValueError):
    pass


class BoundedMemoryStore:
    """Thread-safe bounded store; namespace is part of every key."""

    def __init__(self, limits: MemoryLimits | None = None) -> None:
        self._limits = MemoryLimits.model_validate(
            (limits or MemoryLimits()).model_dump()
        )
        self._facts: OrderedDict[tuple[str, str, str, str], MemoryFact] = (
            OrderedDict()
        )
        self._bytes = 0
        self._lock = threading.RLock()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._facts)

    @property
    def bytes_used(self) -> int:
        with self._lock:
            return self._bytes

    def put(self, fact: MemoryFact, *, now: datetime) -> None:
        verified = MemoryFact.model_validate(fact.model_dump())
        if verified.expires_at <= now:
            raise MemoryStoreError("expired memory fact cannot be stored")
        fact_bytes = len(verified.text.encode("utf-8"))
        if fact_bytes > self._limits.max_bytes:
            raise MemoryStoreError("memory fact exceeds store byte quota")
        key = self._key(verified)
        with self._lock:
            self._evict_expired(now)
            previous = self._facts.pop(key, None)
            if previous is not None:
                self._bytes -= self._fact_bytes(previous)
            self._facts[key] = verified
            self._bytes += fact_bytes
            self._evict_to_limits()
            if key not in self._facts:
                raise MemoryStoreError("memory fact was evicted by quota")

    def get(
        self,
        namespace: MemoryNamespace,
        fact_id: SourceId,
        *,
        now: datetime,
    ) -> MemoryFact | None:
        key = self._key_for(namespace, fact_id)
        with self._lock:
            self._evict_expired(now)
            fact = self._facts.get(key)
            if fact is None or fact.verification is not MemoryVerification.VERIFIED:
                return None
            self._facts.move_to_end(key)
            return fact

    def retrieve(
        self,
        namespace: MemoryNamespace,
        query: MemoryQuery,
        *,
        now: datetime,
    ) -> MemoryRetrieval:
        verified_namespace = MemoryNamespace.model_validate(namespace.model_dump())
        verified_query = MemoryQuery.model_validate(query.model_dump())
        deadline = time.monotonic() + verified_query.deadline_ms / 1000
        with self._lock:
            self._evict_expired(now)
            candidates = tuple(
                fact
                for fact in self._facts.values()
                if fact.namespace == verified_namespace
                and fact.verification is MemoryVerification.VERIFIED
            )
            candidates = tuple(
                sorted(candidates, key=lambda fact: (-fact.relevance, fact.fact_id))
            )
            selected: list[MemoryFact] = []
            total_bytes = 0
            timed_out = False
            for fact in candidates:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                fact_bytes = self._fact_bytes(fact)
                if total_bytes + fact_bytes > verified_query.max_bytes:
                    continue
                selected.append(fact)
                total_bytes += fact_bytes
                if len(selected) >= verified_query.limit:
                    break
            return MemoryRetrieval(
                facts=tuple(selected),
                total_bytes=total_bytes,
                timed_out=timed_out,
            )

    def _evict_expired(self, now: datetime) -> None:
        expired = tuple(
            key for key, fact in self._facts.items() if fact.expires_at <= now
        )
        for key in expired:
            fact = self._facts.pop(key)
            self._bytes -= self._fact_bytes(fact)

    def _evict_to_limits(self) -> None:
        while (
            len(self._facts) > self._limits.max_entries
            or self._bytes > self._limits.max_bytes
        ):
            _, fact = self._facts.popitem(last=False)
            self._bytes -= self._fact_bytes(fact)

    @staticmethod
    def _fact_bytes(fact: MemoryFact) -> int:
        return len(fact.text.encode("utf-8"))

    @staticmethod
    def _key(fact: MemoryFact) -> tuple[str, str, str, str]:
        return BoundedMemoryStore._key_for(fact.namespace, fact.fact_id)

    @staticmethod
    def _key_for(
        namespace: MemoryNamespace,
        fact_id: SourceId,
    ) -> tuple[str, str, str, str]:
        return (
            namespace.tenant_id,
            namespace.workspace_id,
            namespace.principal_id,
            fact_id,
        )
