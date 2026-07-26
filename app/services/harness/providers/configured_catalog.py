"""Immutable bounded model catalog built from validated provider configuration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Never

from app.services.harness.protocol import (
    ProviderCatalogPage,
    ProviderCatalogPageRequest,
    ProviderCatalogSummary,
    ProviderListPage,
    ProviderListPageRequest,
    ProviderModelCapabilities,
)
from app.services.harness.providers.catalog_cursor import (
    CatalogCursorError,
    CatalogCursorKind,
    decode_catalog_cursor,
    encode_catalog_cursor,
)
from app.services.harness.providers.config_contracts import (
    LoadedProviderConfiguration,
)


class ConfiguredCatalogErrorCode(StrEnum):
    CANCELLED = "cancelled"
    DEADLINE = "deadline"
    INVALID_CURSOR = "invalid_cursor"
    UNKNOWN_PROVIDER = "unknown_provider"


class ConfiguredCatalogError(RuntimeError):
    def __init__(self, code: ConfiguredCatalogErrorCode) -> None:
        super().__init__("configured provider catalog operation rejected")
        self.code = code


class ConfiguredModelCatalog:
    def __init__(
        self,
        loaded: LoadedProviderConfiguration,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        models_by_provider: dict[
            str,
            list[ProviderModelCapabilities],
        ] = {}
        for model in loaded.configuration.models:
            models_by_provider.setdefault(model.provider, []).append(model)
        self._models_by_provider = {
            provider: tuple(models)
            for provider, models in models_by_provider.items()
        }
        self._configuration_sha256 = loaded.content_sha256
        self._clock = clock
        summaries: list[ProviderCatalogSummary] = []
        for provider in sorted(self._models_by_provider):
            models = self._models_by_provider[provider]
            summaries.append(
                ProviderCatalogSummary(
                    provider=provider,
                    catalog_snapshot_sha256=(
                        models[0].catalog_snapshot_sha256
                    ),
                    model_count=len(models),
                    observed_at=max(
                        model.observed_at for model in models
                    ),
                )
            )
        self._providers = tuple(summaries)

    async def list_providers(
        self,
        request: ProviderListPageRequest,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> ProviderListPage:
        self._require_active(cancellation, deadline_at)
        offset = self._cursor_offset(
            request.cursor,
            expected_kind=CatalogCursorKind.PROVIDERS,
            expected_snapshot=self._configuration_sha256,
            expected_provider=None,
            item_count=len(self._providers),
        )
        end = min(offset + request.limit, len(self._providers))
        has_more = end < len(self._providers)
        next_cursor = (
            encode_catalog_cursor(
                kind=CatalogCursorKind.PROVIDERS,
                offset=end,
                snapshot_sha256=self._configuration_sha256,
            )
            if has_more
            else None
        )
        return ProviderListPage(
            providers=self._providers[offset:end],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    async def page(
        self,
        request: ProviderCatalogPageRequest,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> ProviderCatalogPage:
        self._require_active(cancellation, deadline_at)
        models = self._models_by_provider.get(request.provider)
        if models is None:
            self._reject(ConfiguredCatalogErrorCode.UNKNOWN_PROVIDER)
        snapshot_sha256 = models[0].catalog_snapshot_sha256
        offset = self._cursor_offset(
            request.cursor,
            expected_kind=CatalogCursorKind.MODELS,
            expected_snapshot=snapshot_sha256,
            expected_provider=request.provider,
            item_count=len(models),
        )
        end = min(offset + request.limit, len(models))
        has_more = end < len(models)
        next_cursor = (
            encode_catalog_cursor(
                kind=CatalogCursorKind.MODELS,
                offset=end,
                snapshot_sha256=snapshot_sha256,
                provider=request.provider,
            )
            if has_more
            else None
        )
        return ProviderCatalogPage(
            provider=request.provider,
            catalog_snapshot_sha256=snapshot_sha256,
            models=models[offset:end],
            next_cursor=next_cursor,
            has_more=has_more,
            observed_at=max(model.observed_at for model in models),
        )

    def _cursor_offset(
        self,
        cursor: str | None,
        *,
        expected_kind: CatalogCursorKind,
        expected_snapshot: str,
        expected_provider: str | None,
        item_count: int,
    ) -> int:
        if cursor is None:
            return 0
        try:
            payload = decode_catalog_cursor(cursor)
        except CatalogCursorError:
            self._reject(ConfiguredCatalogErrorCode.INVALID_CURSOR)
        invalid = (
            payload.kind is not expected_kind
            or payload.snapshot_sha256 != expected_snapshot
            or payload.provider != expected_provider
            or payload.offset >= item_count
        )
        if invalid:
            self._reject(ConfiguredCatalogErrorCode.INVALID_CURSOR)
        return payload.offset

    def _require_active(
        self,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> None:
        if cancellation.is_set():
            self._reject(ConfiguredCatalogErrorCode.CANCELLED)
        now = self._clock()
        if (
            now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or deadline_at.tzinfo is None
            or deadline_at.utcoffset() != timedelta(0)
            or deadline_at <= now
        ):
            self._reject(ConfiguredCatalogErrorCode.DEADLINE)

    @staticmethod
    def _reject(code: ConfiguredCatalogErrorCode) -> Never:
        raise ConfiguredCatalogError(code)
