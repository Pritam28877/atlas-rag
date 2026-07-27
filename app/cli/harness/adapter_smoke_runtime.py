"""Shared admission, route, and cost helpers for adapter live smokes."""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Mapping
from datetime import datetime

from app.cli.harness.adapter_smoke_contracts import AuthorizedAdapterSmoke
from app.services.harness.journal import SQLiteProviderCostLedger
from app.services.harness.protocol import (
    DataClassification,
    ProviderCostLimits,
    ProviderCostReleaseRequest,
    ProviderCostReservationRequest,
    ProviderCostSettlementRequest,
    ProviderModelCapabilities,
)
from app.services.harness.providers import (
    LoadedProviderConfiguration,
    ProviderRouteConfiguration,
)

ADAPTER_SMOKE_GATE_VALUE = b"enabled"


def require_adapter_smoke_gate(
    authorized: AuthorizedAdapterSmoke,
    environment: Mapping[bytes, bytes] | None,
) -> None:
    selected_environment = (
        os.environb if environment is None else environment
    )
    name = authorized.gate_environment_variable.encode("ascii")
    if selected_environment.get(name) != ADAPTER_SMOKE_GATE_VALUE:
        raise ValueError("adapter live smoke environment gate is closed")


def select_adapter_smoke_route(
    authorized: AuthorizedAdapterSmoke,
    loaded: LoadedProviderConfiguration,
) -> tuple[ProviderRouteConfiguration, ProviderModelCapabilities]:
    routes = tuple(
        route
        for route in loaded.configuration.routes
        if (
            route.enabled
            and route.provider == authorized.provider
            and route.model == authorized.model
        )
    )
    if len(routes) != 1:
        raise ValueError("adapter smoke requires one enabled route")
    route = routes[0]
    models = tuple(
        model
        for model in loaded.configuration.models
        if (
            model.provider == route.provider
            and model.model == route.model
            and model.model_revision_sha256
            == route.model_revision_sha256
        )
    )
    policies = tuple(
        policy
        for policy in loaded.configuration.data_policies
        if (
            policy.provider == route.provider
            and policy.policy_revision_sha256
            == route.policy_revision_sha256
        )
    )
    if (
        len(models) != 1
        or len(policies) != 1
        or authorized.max_output_tokens > models[0].max_output_tokens
        or DataClassification.PUBLIC
        not in policies[0].accepted_classifications
        or policies[0].retention_days != 0
        or policies[0].training_enabled
    ):
        raise ValueError("adapter smoke route policy is ineligible")
    return route, models[0]


def smoke_identifier(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def smoke_binding_sha256(*records: bytes) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(hashlib.sha256(record).digest())
    return digest.hexdigest()


class AdapterSmokeCostTracker:
    def __init__(
        self,
        ledger: SQLiteProviderCostLedger,
        *,
        workspace_id: str,
        turn_id: str,
        request_id: str,
        provider_request_sha256: str,
        cost_cap_microusd: int,
    ) -> None:
        self._ledger = ledger
        self._workspace_id = workspace_id
        self._turn_id = turn_id
        self._request_id = request_id
        self._request_sha256 = provider_request_sha256
        self._cost_cap = cost_cap_microusd
        self._reservation_id = smoke_identifier("pcs")
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    async def reserve(self, observed_at: datetime) -> None:
        decision = await self._ledger.reserve(
            ProviderCostReservationRequest(
                reservation_id=self._reservation_id,
                workspace_id=self._workspace_id,
                turn_id=self._turn_id,
                request_id=self._request_id,
                provider_request_sha256=self._request_sha256,
                attempt=1,
                estimated_cost_microusd=self._cost_cap,
                limits=ProviderCostLimits(
                    max_call_microusd=max(1, self._cost_cap),
                    max_turn_microusd=max(1, self._cost_cap),
                    max_workspace_microusd=max(1, self._cost_cap),
                ),
                requested_at=observed_at,
            )
        )
        if not decision.allowed or decision.already_exists:
            raise ValueError("adapter smoke cost reservation was denied")
        self._active = True

    async def settle(self, observed_at: datetime) -> None:
        if not self._active:
            raise ValueError("adapter smoke cost reservation is inactive")
        await self._ledger.settle(
            ProviderCostSettlementRequest(
                reservation_id=self._reservation_id,
                provider_request_sha256=self._request_sha256,
                actual_cost_microusd=self._cost_cap,
                settled_at=observed_at,
            )
        )
        self._active = False

    async def release(self, observed_at: datetime) -> None:
        if not self._active:
            return
        await self._ledger.release(
            ProviderCostReleaseRequest(
                reservation_id=self._reservation_id,
                provider_request_sha256=self._request_sha256,
                released_at=observed_at,
            )
        )
        self._active = False
