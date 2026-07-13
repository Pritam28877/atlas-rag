from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import get_settings
from app.core.readiness import InfrastructureReadiness
from app.core.telemetry import Telemetry, configure_structured_logging


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
        try:
            yield
        finally:
            telemetry.close()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.logger = configure_structured_logging(settings.telemetry)
    app.state.readiness = InfrastructureReadiness(settings)
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
