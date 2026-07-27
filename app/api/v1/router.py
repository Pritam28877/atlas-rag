from fastapi import APIRouter

from app.api.v1.catalog import router as catalog_router
from app.api.v1.harness.policy import create_policy_router
from app.api.v1.health import router as health_router
from app.api.v1.metrics import router as metrics_router

router = APIRouter()
router.include_router(catalog_router)
router.include_router(health_router)
router.include_router(create_policy_router())
router.include_router(metrics_router)
