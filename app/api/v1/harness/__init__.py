"""Authenticated Atlas Harness API composition boundary."""

from fastapi import APIRouter

from app.api.v1.harness.policy import create_policy_router


def create_harness_router() -> APIRouter:
    router = APIRouter()
    router.include_router(create_policy_router())
    return router


__all__ = ("create_harness_router",)
