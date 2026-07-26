"""Secret-free request and policy builders for live provider smokes."""

from __future__ import annotations

import hashlib
from datetime import datetime

from app.cli.harness.provider_smoke_contracts import AuthorizedProviderSmoke
from app.cli.harness.provider_smoke_decode import EXPECTED_SMOKE_RESPONSE
from app.services.harness.protocol import (
    CanonicalProviderRequest,
    DataClassification,
    InlinePayload,
    ProviderContentPart,
    ProviderContextPlan,
    ProviderMessage,
    ProviderMessageRole,
    ProviderModality,
    ProviderModelCapabilities,
)
from app.services.harness.providers import (
    LoadedProviderConfiguration,
    ProviderEgressPolicy,
    ProviderEgressRequest,
    ProviderRouteConfiguration,
    SafeEgressHeader,
    canonical_provider_url,
    provider_destination_sha256,
)

SMOKE_PROMPT = f"Reply with exactly {EXPECTED_SMOKE_RESPONSE}."
MAXIMUM_SMOKE_RESPONSE_BYTES = 256 * 1024


def select_smoke_route(
    authorized: AuthorizedProviderSmoke,
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
        raise ValueError("provider smoke requires one matching enabled route")
    route = routes[0]
    models = tuple(
        model
        for model in loaded.configuration.models
        if (
            model.provider == route.provider
            and model.model == route.model
            and model.model_revision_sha256 == route.model_revision_sha256
        )
    )
    if len(models) != 1:
        raise ValueError("provider smoke model revision is ambiguous")
    if authorized.max_output_tokens > models[0].max_output_tokens:
        raise ValueError("provider smoke output cap exceeds model capability")
    _validate_route_policy(authorized, loaded, route)
    return route, models[0]


def build_canonical_smoke_request(
    route: ProviderRouteConfiguration,
    *,
    request_id: str,
    turn_id: str,
    deadline_at: datetime,
    max_output_tokens: int,
) -> CanonicalProviderRequest:
    prompt = SMOKE_PROMPT.encode()
    return CanonicalProviderRequest(
        request_id=request_id,
        turn_id=turn_id,
        route_id=route.route_id,
        classification=DataClassification.PUBLIC,
        messages=(
            ProviderMessage(
                role=ProviderMessageRole.USER,
                parts=(
                    ProviderContentPart(
                        modality=ProviderModality.TEXT,
                        payload=InlinePayload(
                            text=SMOKE_PROMPT,
                            size_bytes=len(prompt),
                            content_sha256=hashlib.sha256(prompt).hexdigest(),
                        ),
                    ),
                ),
            ),
        ),
        tools=(),
        required_capabilities=(),
        output_modalities=(ProviderModality.TEXT,),
        reserved_output_tokens=max_output_tokens,
        reserved_reasoning_tokens=0,
        deadline_at=deadline_at,
    )


def build_smoke_context(
    request: CanonicalProviderRequest,
    model: ProviderModelCapabilities,
) -> ProviderContextPlan:
    request_content = request.model_dump_json().encode()
    return ProviderContextPlan(
        provider_request_sha256=hashlib.sha256(request_content).hexdigest(),
        model_revision_sha256=model.model_revision_sha256,
        applied_features=(),
        estimated_input_tokens=8,
        reason="Live provider smoke uses bounded stateless context.",
    )


def build_smoke_egress(
    authorized: AuthorizedProviderSmoke,
    route: ProviderRouteConfiguration,
    *,
    request_id: str,
    body: bytes,
    safe_headers: tuple[SafeEgressHeader, ...],
) -> tuple[ProviderEgressRequest, ProviderEgressPolicy]:
    destination_sha256 = provider_destination_sha256(authorized.destination_url)
    target_url = _responses_url(authorized.destination_url)
    request = ProviderEgressRequest(
        request_id=request_id,
        provider=route.provider,
        credential_handle=route.credential_handle,
        target_url=target_url,
        classification=DataClassification.PUBLIC,
        content_type="application/json",
        safe_headers=safe_headers,
        body=body,
    )
    policy = ProviderEgressPolicy(
        provider=route.provider,
        destination_url=authorized.destination_url,
        destination_sha256=destination_sha256,
        allowed_redirect_origins=(),
        accepted_classifications=(DataClassification.PUBLIC,),
        max_request_bytes=len(body),
        max_response_bytes=MAXIMUM_SMOKE_RESPONSE_BYTES,
        max_redirects=0,
    )
    return request, policy


def _validate_route_policy(
    authorized: AuthorizedProviderSmoke,
    loaded: LoadedProviderConfiguration,
    route: ProviderRouteConfiguration,
) -> None:
    destination_sha256 = provider_destination_sha256(authorized.destination_url)
    binding = next(
        binding
        for binding in loaded.configuration.credential_bindings
        if binding.handle == route.credential_handle
    )
    policy = next(
        policy
        for policy in loaded.configuration.data_policies
        if (
            policy.provider == route.provider
            and policy.policy_revision_sha256 == route.policy_revision_sha256
        )
    )
    if (
        binding.destination_sha256 != destination_sha256
        or DataClassification.PUBLIC not in policy.accepted_classifications
        or policy.retention_days != 0
        or policy.training_enabled
    ):
        raise ValueError("provider smoke route policy is not eligible")


def _responses_url(destination_url: str) -> str:
    if destination_url.rstrip("/").endswith("/responses"):
        return destination_url
    return canonical_provider_url(f"{destination_url.rstrip('/')}/responses")
