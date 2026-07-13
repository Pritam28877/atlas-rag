"""Bounded readiness checks for independently configured infrastructure."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.core.config import Settings
from app.core.database import Database
from app.core.storage import ObjectStorage
from app.workers.celery_app import check_broker_connection, check_worker_connection


class DependencyStatus(StrEnum):
    READY = "ready"
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ReadinessReport:
    """Safe dependency state without URLs, credentials, or exception text."""

    ready: bool
    dependencies: Mapping[str, DependencyStatus]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "ready" if self.ready else "unavailable",
            "dependencies": dict(self.dependencies),
        }


ReadinessCheck = Callable[[], Awaitable[object]]
NOT_CONFIGURED = object()


class InfrastructureReadiness:
    """Check configured platform dependencies concurrently with bounded clients."""

    def __init__(
        self,
        settings: Settings,
        checks: Mapping[str, ReadinessCheck] | None = None,
    ) -> None:
        self._settings = settings
        self._checks = checks

    async def check(self) -> ReadinessReport:
        """Return safe readiness classifications for every platform dependency."""
        checks = self._checks or self._configured_checks()
        results = await asyncio.gather(
            *(check() for check in checks.values()),
            return_exceptions=True,
        )
        dependencies: dict[str, DependencyStatus] = {}
        for name, result in zip(checks, results, strict=True):
            if result is NOT_CONFIGURED:
                dependencies[name] = DependencyStatus.NOT_CONFIGURED
            elif result is None:
                dependencies[name] = DependencyStatus.READY
            else:
                dependencies[name] = DependencyStatus.UNAVAILABLE
        return ReadinessReport(
            ready=all(
                status is not DependencyStatus.UNAVAILABLE
                for status in dependencies.values()
            ),
            dependencies=dependencies,
        )

    def _configured_checks(self) -> Mapping[str, ReadinessCheck]:
        checks: dict[str, ReadinessCheck] = {}
        if self._settings.database.url is not None:
            checks["database"] = self._check_database
        if self._storage_is_configured():
            checks["storage"] = self._check_storage
        elif self._storage_has_any_configuration():
            checks["storage"] = self._misconfigured
        if self._settings.broker.url is not None:
            checks["broker"] = self._check_broker
            checks["workers"] = self._check_workers

        statuses = {
            "database": self._settings.database.url is not None,
            "storage": self._storage_is_configured()
            or self._storage_has_any_configuration(),
            "broker": self._settings.broker.url is not None,
            "workers": self._settings.broker.url is not None,
        }
        for name, configured in statuses.items():
            if not configured:
                checks[name] = self._not_configured
        return checks

    def _storage_is_configured(self) -> bool:
        return all(self._storage_configuration_values())

    def _storage_has_any_configuration(self) -> bool:
        return any(self._storage_configuration_values())

    def _storage_configuration_values(self) -> list[object | None]:
        storage = self._settings.storage
        return [
            storage.endpoint_url,
            storage.bucket_name,
            storage.access_key_id,
            storage.secret_access_key,
        ]

    async def _check_database(self) -> None:
        database = Database(self._settings.database)
        try:
            await database.check_connection()
        finally:
            await database.close()

    async def _check_storage(self) -> None:
        storage = ObjectStorage(
            self._settings.storage,
            provider_timeouts=self._settings.provider_timeouts,
        )
        try:
            await storage.check_connection()
        finally:
            storage.close()

    async def _check_broker(self) -> None:
        await asyncio.to_thread(check_broker_connection, self._settings)

    async def _check_workers(self) -> None:
        await asyncio.to_thread(check_worker_connection, self._settings)

    async def _not_configured(self) -> object:
        return NOT_CONFIGURED

    async def _misconfigured(self) -> None:
        raise RuntimeError("storage configuration is incomplete")
