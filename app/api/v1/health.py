import json

from fastapi import APIRouter, Request, Response

from app.core.readiness import InfrastructureReadiness

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Return a lightweight process health signal."""
    return {"status": "ok"}


@router.get("/ready")
async def readiness_check(request: Request) -> Response:
    """Report safe configured-dependency readiness without leaking internals."""
    readiness: InfrastructureReadiness = request.app.state.readiness
    report = await readiness.check()
    return Response(
        content=json.dumps(report.as_dict()),
        media_type="application/json",
        status_code=200 if report.ready else 503,
    )
