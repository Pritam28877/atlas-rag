import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.services.harness.protocol import (
    ModelCatalog,
    ProviderCatalogPageRequest,
    ProviderListPageRequest,
)
from app.services.harness.providers import (
    ConfiguredCatalogError,
    ConfiguredCatalogErrorCode,
    ConfiguredModelCatalog,
)
from tests.harness_configured_catalog_fixtures import (
    configured_catalog_fixture,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def test_provider_and_model_lists_are_bounded_and_paginated() -> None:
    async def scenario() -> None:
        catalog: ModelCatalog = ConfiguredModelCatalog(
            configured_catalog_fixture(),
            clock=lambda: NOW,
        )
        provider_first = await catalog.list_providers(
            ProviderListPageRequest(limit=1),
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=1),
        )
        assert provider_first.next_cursor is not None
        provider_second = await catalog.list_providers(
            ProviderListPageRequest(
                cursor=provider_first.next_cursor,
                limit=1,
            ),
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=1),
        )
        model_first = await catalog.page(
            ProviderCatalogPageRequest(
                provider="configured-provider",
                limit=1,
            ),
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=1),
        )
        assert model_first.next_cursor is not None
        model_second = await catalog.page(
            ProviderCatalogPageRequest(
                provider="configured-provider",
                cursor=model_first.next_cursor,
                limit=1,
            ),
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=1),
        )

        assert tuple(
            item.provider
            for item in provider_first.providers
            + provider_second.providers
        ) == ("configured-provider", "secondary-provider")
        assert provider_first.providers[0].model_count == 2
        assert not provider_second.has_more
        assert tuple(
            item.model
            for item in model_first.models + model_second.models
        ) == ("configured-model", "configured-model-v2")
        assert not model_second.has_more
        serialized_pages = (
            provider_first.model_dump_json()
            + provider_second.model_dump_json()
            + model_first.model_dump_json()
            + model_second.model_dump_json()
        )
        assert "pcr_" not in serialized_pages
        assert "destination" not in serialized_pages

    asyncio.run(scenario())


def test_unknown_provider_cancellation_and_deadline_fail_explicitly() -> None:
    async def scenario() -> None:
        catalog = ConfiguredModelCatalog(
            configured_catalog_fixture(),
            clock=lambda: NOW,
        )
        with pytest.raises(ConfiguredCatalogError) as provider_error:
            await catalog.page(
                ProviderCatalogPageRequest(provider="unknown-provider"),
                cancellation=asyncio.Event(),
                deadline_at=NOW + timedelta(seconds=1),
            )
        cancellation = asyncio.Event()
        cancellation.set()
        with pytest.raises(ConfiguredCatalogError) as cancellation_error:
            await catalog.list_providers(
                ProviderListPageRequest(),
                cancellation=cancellation,
                deadline_at=NOW + timedelta(seconds=1),
            )
        with pytest.raises(ConfiguredCatalogError) as deadline_error:
            await catalog.list_providers(
                ProviderListPageRequest(),
                cancellation=asyncio.Event(),
                deadline_at=NOW,
            )

        assert provider_error.value.code is (
            ConfiguredCatalogErrorCode.UNKNOWN_PROVIDER
        )
        assert cancellation_error.value.code is (
            ConfiguredCatalogErrorCode.CANCELLED
        )
        assert deadline_error.value.code is (
            ConfiguredCatalogErrorCode.DEADLINE
        )

    asyncio.run(scenario())


def test_cursors_reject_tampering_scope_and_stale_snapshots() -> None:
    async def invalid_cursor(
        catalog: ConfiguredModelCatalog,
        request: ProviderCatalogPageRequest,
    ) -> None:
        with pytest.raises(ConfiguredCatalogError) as captured:
            await catalog.page(
                request,
                cancellation=asyncio.Event(),
                deadline_at=NOW + timedelta(seconds=1),
            )
        assert captured.value.code is ConfiguredCatalogErrorCode.INVALID_CURSOR

    async def scenario() -> None:
        catalog = ConfiguredModelCatalog(
            configured_catalog_fixture(),
            clock=lambda: NOW,
        )
        models = await catalog.page(
            ProviderCatalogPageRequest(
                provider="configured-provider",
                limit=1,
            ),
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=1),
        )
        providers = await catalog.list_providers(
            ProviderListPageRequest(limit=1),
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=1),
        )
        assert models.next_cursor is not None
        assert providers.next_cursor is not None
        tampered = f"{models.next_cursor[:-1]}A"
        await invalid_cursor(
            catalog,
            ProviderCatalogPageRequest(
                provider="configured-provider",
                cursor=tampered,
            ),
        )
        await invalid_cursor(
            catalog,
            ProviderCatalogPageRequest(
                provider="secondary-provider",
                cursor=models.next_cursor,
            ),
        )
        await invalid_cursor(
            catalog,
            ProviderCatalogPageRequest(
                provider="configured-provider",
                cursor=providers.next_cursor,
            ),
        )
        reloaded = ConfiguredModelCatalog(
            configured_catalog_fixture(
                primary_snapshot_sha256="e" * 64,
            ),
            clock=lambda: NOW,
        )
        await invalid_cursor(
            reloaded,
            ProviderCatalogPageRequest(
                provider="configured-provider",
                cursor=models.next_cursor,
            ),
        )

    asyncio.run(scenario())
