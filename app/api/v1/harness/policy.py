"""Authenticated read-only policy lint and explanation routes."""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError

from app.core.auth import Principal, require_principal
from app.services.harness.policy import (
    EffectivePolicyDecision,
    PolicyBundle,
    PolicyDecisionExplanation,
    PolicyLintReport,
    explain_policy_decision,
    lint_policy_bundle,
)

AuthenticatedPrincipal = Annotated[Principal, Depends(require_principal)]


def create_policy_router() -> APIRouter:
    router = APIRouter(
        prefix="/harness/policy",
        tags=["atlas-harness-policy"],
    )

    @router.post("/lint", response_model=PolicyLintReport)
    async def lint_policy(
        body: dict[str, object],
        _principal: AuthenticatedPrincipal,
    ) -> PolicyLintReport:
        policy_bundle = _parse_policy_bundle(body)
        return lint_policy_bundle(policy_bundle)

    @router.post("/explain", response_model=PolicyDecisionExplanation)
    async def explain_policy(
        body: dict[str, object],
        _principal: AuthenticatedPrincipal,
    ) -> PolicyDecisionExplanation:
        decision = _parse_policy_decision(body)
        return explain_policy_decision(decision)

    return router


def _parse_policy_bundle(body: dict[str, object]) -> PolicyBundle:
    try:
        return PolicyBundle.model_validate_json(_canonical_json(body))
    except ValidationError:
        raise _invalid_policy_request() from None


def _parse_policy_decision(
    body: dict[str, object],
) -> EffectivePolicyDecision:
    try:
        return EffectivePolicyDecision.model_validate_json(_canonical_json(body))
    except ValidationError:
        raise _invalid_policy_request() from None


def _canonical_json(body: dict[str, object]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _invalid_policy_request() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "code": "INVALID_POLICY_REQUEST",
            "message": "policy request is invalid",
        },
    )
