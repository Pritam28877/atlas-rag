"""Bounded provider and model catalog pagination records."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    Cursor,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.provider_capabilities import (
    ProviderModelCapabilities,
)
from app.services.harness.protocol.routing import ProviderName


class ProviderListPageRequest(StrictProtocolModel):
    cursor: Cursor | None = None
    limit: int = Field(default=50, ge=1, le=64)


class ProviderCatalogSummary(StrictProtocolModel):
    provider: ProviderName
    catalog_snapshot_sha256: Sha256
    model_count: int = Field(ge=1, le=1_024)
    observed_at: UtcTimestamp


class ProviderListPage(StrictProtocolModel):
    providers: tuple[ProviderCatalogSummary, ...] = Field(max_length=64)
    next_cursor: Cursor | None = None
    has_more: bool

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("provider cursor must be present exactly with more")
        provider_names = tuple(item.provider for item in self.providers)
        if tuple(sorted(set(provider_names))) != provider_names:
            raise ValueError("catalog providers must be unique and sorted")
        return self


class ProviderCatalogPageRequest(StrictProtocolModel):
    provider: ProviderName
    cursor: Cursor | None = None
    limit: int = Field(default=50, ge=1, le=200)


class ProviderCatalogPage(StrictProtocolModel):
    provider: ProviderName
    catalog_snapshot_sha256: Sha256
    models: tuple[ProviderModelCapabilities, ...] = Field(max_length=200)
    next_cursor: Cursor | None = None
    has_more: bool
    observed_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("catalog cursor must be present exactly with more")
        model_keys = tuple(
            (model.model, model.model_revision_sha256)
            for model in self.models
        )
        if tuple(sorted(set(model_keys))) != model_keys:
            raise ValueError("catalog models must be unique and sorted")
        for model in self.models:
            if model.provider != self.provider:
                raise ValueError("catalog page contains another provider")
            if model.catalog_snapshot_sha256 != self.catalog_snapshot_sha256:
                raise ValueError("catalog model snapshot does not match page")
        return self
