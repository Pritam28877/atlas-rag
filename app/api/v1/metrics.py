import secrets

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.core.config import Settings
from app.core.telemetry import MetricsUnavailableError, Telemetry

router = APIRouter(include_in_schema=False)


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    """Expose bounded operational metrics for Prometheus scraping."""
    settings: Settings = request.app.state.settings
    if not settings.telemetry.metrics_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    _authorize_metrics(request, settings)
    telemetry: Telemetry = request.app.state.telemetry
    database = getattr(request.app.state, "database", None)
    if database is not None:
        try:
            await telemetry.refresh_from_database(database)
        except MetricsUnavailableError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="metrics are temporarily unavailable",
            ) from error
    return Response(
        content=telemetry.metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


def _authorize_metrics(request: Request, settings: Settings) -> None:
    configured = settings.telemetry.metrics_bearer_token
    if configured is None:
        return
    authorization = request.headers.get("authorization", "")
    scheme, separator, supplied = authorization.partition(" ")
    expected = configured.get_secret_value()
    valid = (
        separator == " "
        and scheme.lower() == "bearer"
        and len(supplied) <= 4096
        and secrets.compare_digest(supplied, expected)
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="metrics authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
