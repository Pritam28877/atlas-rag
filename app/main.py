from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI

from app.api.router import api_router
from app.core.auth import OidcVerifier
from app.core.config import get_settings
from app.core.database import Database
from app.core.readiness import InfrastructureReadiness
from app.core.request_limits import RequestBodyLimitMiddleware
from app.core.storage import ObjectStorage
from app.core.telemetry import Telemetry, configure_structured_logging
from app.services.catalog.service import CatalogService, CeleryJobDispatcher
from app.workers.celery_app import (
    IngestionTaskDispatcher,
    TaskSender,
    WorkerKind,
    create_celery_app,
)


def create_app() -> FastAPI:
    """Build the FastAPI application with its versioned API routes."""
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        telemetry = Telemetry(
            settings.telemetry,
            provider_timeouts=settings.provider_timeouts,
        )
        app.state.telemetry = telemetry
        auth = None
        database = None
        storage = None
        celery = None
        if settings.auth.issuer:
            auth = OidcVerifier(settings.auth, settings.provider_timeouts)
            app.state.auth = auth
        catalog_dependencies = (
            settings.database.url,
            settings.storage.endpoint_url,
            settings.storage.bucket_name,
            settings.storage.access_key_id,
            settings.storage.secret_access_key,
            settings.broker.url,
        )
        if all(catalog_dependencies):
            database = Database(settings.database)
            app.state.database = database
            storage = ObjectStorage(
                settings.storage,
                provider_timeouts=settings.provider_timeouts,
            )
            app.state.storage = storage
            celery = create_celery_app(settings, WorkerKind.NATIVE)
            task_dispatcher = IngestionTaskDispatcher(
                cast(TaskSender, celery),
                settings.broker,
            )
            app.state.catalog_service = CatalogService(
                database,
                storage,
                CeleryJobDispatcher(task_dispatcher),
                settings,
            )
        try:
            yield
        finally:
            if auth is not None:
                await auth.close()
            if database is not None:
                await database.close()
            if storage is not None:
                storage.close()
            if celery is not None:
                celery.close()
            telemetry.close()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        RequestBodyLimitMiddleware,
        maximum_bytes=settings.api_request_max_bytes,
    )
    app.state.logger = configure_structured_logging(settings.telemetry)
    app.state.readiness = InfrastructureReadiness(settings)
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
