"""Authenticated secret-free policy API tests."""

from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.harness.policy import create_policy_router
from app.core.auth import Principal
from app.services.harness.policy import PolicyEffect, PolicyLayer
from tests.harness.policy.approval_fixtures import asked_decision
from tests.harness.policy.fixtures import bundle, document, rule


class Verifier:
    async def verify(self, token: str) -> Principal:
        return Principal(
            subject=f"subject:{token}",
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        )


def client() -> TestClient:
    app = FastAPI()
    app.state.auth = Verifier()
    app.include_router(create_policy_router())
    return TestClient(app)


def test_policy_routes_require_authentication() -> None:
    response = client().post(
        "/harness/policy/lint",
        json=bundle(
            document(
                PolicyLayer.SYSTEM,
                rule("allow-rule", PolicyEffect.ALLOW),
            )
        ).model_dump(mode="json"),
    )

    assert response.status_code == 401


def test_lint_route_returns_bounded_findings() -> None:
    response = client().post(
        "/harness/policy/lint",
        headers={"Authorization": "Bearer test-token"},
        json=bundle(
            document(
                PolicyLayer.SYSTEM,
                rule(
                    "wide-rule",
                    PolicyEffect.ASK,
                    exact_target=False,
                ),
            )
        ).model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert response.json()["findings"][0]["code"] == "overbroad_target"


def test_explanation_route_omits_targets_arguments_and_secret_handles() -> None:
    decision = asked_decision()
    response = client().post(
        "/harness/policy/explain",
        headers={"Authorization": "Bearer test-token"},
        json=decision.model_dump(mode="json"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["layers"][0]["matched_rule_ids"] == ["approval-rule"]
    assert body["evaluated_rules"][0]["rule_id"] == "approval-rule"
    serialized = response.text
    assert "src/app.py" not in serialized
    assert "secret:" not in serialized
    assert decision.request_sha256 not in serialized


def test_invalid_request_does_not_echo_secret_bearing_input() -> None:
    response = client().post(
        "/harness/policy/explain",
        headers={"Authorization": "Bearer test-token"},
        json={"secret_handle": "secret:must-not-echo"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_POLICY_REQUEST"
    assert "must-not-echo" not in response.text
