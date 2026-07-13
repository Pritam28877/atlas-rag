from fastapi import APIRouter, Request, Response

from app.core.telemetry import Telemetry

router = APIRouter(include_in_schema=False)


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    """Expose bounded operational metrics for Prometheus scraping."""
    telemetry: Telemetry = request.app.state.telemetry
    return Response(
        content=telemetry.metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
