"""Pure, deterministic provider route selection with complete evidence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum

from app.services.harness.protocol import (
    MAXIMUM_PROVIDER_DECISION_ROUTES,
    ProviderDecisionId,
    ProviderRequirements,
    ProviderRoute,
    ProviderRouteDecisionRecord,
    ProviderRouteRejectionCode,
    RejectedProviderRoute,
    RouteHealth,
    TurnId,
)

MAXIMUM_PROVIDER_CANDIDATES = MAXIMUM_PROVIDER_DECISION_ROUTES


class ProviderRoutingErrorCode(StrEnum):
    CANDIDATE_LIMIT = "candidate_limit"
    DUPLICATE_ROUTE = "duplicate_route"


class ProviderRoutingError(ValueError):
    """Stable failure raised before a complete decision can be recorded."""

    def __init__(self, code: ProviderRoutingErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def select_provider_route(
    *,
    provider_decision_id: ProviderDecisionId,
    turn_id: TurnId,
    requirements: ProviderRequirements,
    candidates: Sequence[ProviderRoute],
    decided_at: datetime,
) -> ProviderRouteDecisionRecord:
    """Evaluate every bounded candidate and select one without provider branches."""

    _validate_candidates(candidates)
    eligible_routes: list[ProviderRoute] = []
    rejected_routes: list[RejectedProviderRoute] = []

    for route in candidates:
        rejection_codes = _rejection_codes(requirements, route)
        if rejection_codes:
            rejected_routes.append(_rejected_route(route, rejection_codes))
        else:
            eligible_routes.append(route)

    eligible_routes.sort(key=_eligible_route_id)
    rejected_routes.sort(key=_rejected_route_id)
    selected_route = _select_best_route(eligible_routes)
    return ProviderRouteDecisionRecord(
        provider_decision_id=provider_decision_id,
        turn_id=turn_id,
        requirements=requirements,
        eligible_routes=tuple(eligible_routes),
        rejected_routes=tuple(rejected_routes),
        selected_route_id=(
            selected_route.route_id if selected_route is not None else None
        ),
        selection_reason=_selection_reason(
            selected_route,
            rejected_count=len(rejected_routes),
        ),
        decided_at=decided_at,
    )


def _validate_candidates(candidates: Sequence[ProviderRoute]) -> None:
    if len(candidates) > MAXIMUM_PROVIDER_CANDIDATES:
        raise ProviderRoutingError(
            ProviderRoutingErrorCode.CANDIDATE_LIMIT,
            (
                "provider candidate count exceeds "
                f"{MAXIMUM_PROVIDER_CANDIDATES}"
            ),
        )
    route_ids = tuple(route.route_id for route in candidates)
    if len(set(route_ids)) != len(route_ids):
        raise ProviderRoutingError(
            ProviderRoutingErrorCode.DUPLICATE_ROUTE,
            "provider candidate route IDs must be unique",
        )


def _rejection_codes(
    requirements: ProviderRequirements,
    route: ProviderRoute,
) -> tuple[ProviderRouteRejectionCode, ...]:
    rejection_codes: list[ProviderRouteRejectionCode] = []
    required_context_tokens = (
        requirements.input_tokens + requirements.reserved_output_tokens
    )
    checks = (
        (
            route.health not in {RouteHealth.HEALTHY, RouteHealth.DEGRADED},
            ProviderRouteRejectionCode.HEALTH,
        ),
        (
            route.region not in requirements.allowed_regions,
            ProviderRouteRejectionCode.REGION,
        ),
        (
            route.context_window_tokens < required_context_tokens,
            ProviderRouteRejectionCode.CONTEXT_WINDOW,
        ),
        (
            route.max_output_tokens < requirements.reserved_output_tokens,
            ProviderRouteRejectionCode.OUTPUT_LIMIT,
        ),
        (
            route.estimated_cost_microusd > requirements.max_cost_microusd,
            ProviderRouteRejectionCode.COST,
        ),
        (
            not route.price_active,
            ProviderRouteRejectionCode.PRICE,
        ),
        (
            not _contains_all(
                route.capabilities,
                requirements.required_capabilities,
            ),
            ProviderRouteRejectionCode.CAPABILITY,
        ),
        (
            not _contains_all(
                route.input_modalities,
                requirements.required_input_modalities,
            ),
            ProviderRouteRejectionCode.INPUT_MODALITY,
        ),
        (
            not _contains_all(
                route.output_modalities,
                requirements.required_output_modalities,
            ),
            ProviderRouteRejectionCode.OUTPUT_MODALITY,
        ),
        (
            not _contains_all(
                route.context_features,
                requirements.required_context_features,
            ),
            ProviderRouteRejectionCode.CONTEXT_FEATURE,
        ),
        (
            requirements.data_classification
            not in route.accepted_data_classifications,
            ProviderRouteRejectionCode.DATA_CLASSIFICATION,
        ),
        (
            route.retention_days > requirements.max_retention_days,
            ProviderRouteRejectionCode.RETENTION,
        ),
        (
            route.training_enabled and not requirements.allow_training,
            ProviderRouteRejectionCode.TRAINING,
        ),
    )
    for is_rejected, rejection_code in checks:
        if is_rejected:
            rejection_codes.append(rejection_code)
    return tuple(sorted(rejection_codes))


def _contains_all(
    available_values: tuple[str, ...],
    required_values: tuple[str, ...],
) -> bool:
    return set(required_values).issubset(available_values)


def _rejected_route(
    route: ProviderRoute,
    rejection_codes: tuple[ProviderRouteRejectionCode, ...],
) -> RejectedProviderRoute:
    code_list = ", ".join(code.value for code in rejection_codes)
    return RejectedProviderRoute(
        route_id=route.route_id,
        reason=f"Rejected provider route because: {code_list}.",
        rejection_codes=rejection_codes,
        provider=route.provider,
        model=route.model,
        model_revision_sha256=route.model_revision_sha256,
        health=route.health,
        health_snapshot_sha256=route.health_snapshot_sha256,
        price_version_sha256=route.price_version_sha256,
    )


def _select_best_route(
    eligible_routes: Sequence[ProviderRoute],
) -> ProviderRoute | None:
    if not eligible_routes:
        return None
    return min(eligible_routes, key=_route_rank)


def _route_rank(route: ProviderRoute) -> tuple[int, int, int, str]:
    health_rank = 0 if route.health is RouteHealth.HEALTHY else 1
    return (
        health_rank,
        route.priority,
        route.estimated_cost_microusd,
        route.route_id,
    )


def _eligible_route_id(route: ProviderRoute) -> str:
    return route.route_id


def _rejected_route_id(route: RejectedProviderRoute) -> str:
    return route.route_id


def _selection_reason(
    selected_route: ProviderRoute | None,
    *,
    rejected_count: int,
) -> str:
    if selected_route is None:
        return (
            "No provider route satisfies the complete requirements; "
            f"{rejected_count} configured route(s) rejected."
        )
    return (
        f"Selected {selected_route.route_id} by health, configured priority, "
        "estimated cost, and route ID; "
        f"health={selected_route.health.value}; "
        f"priority={selected_route.priority}; "
        f"estimated_cost_microusd={selected_route.estimated_cost_microusd}; "
        f"model_revision_sha256={selected_route.model_revision_sha256}; "
        f"health_snapshot_sha256={selected_route.health_snapshot_sha256}; "
        f"price_version_sha256={selected_route.price_version_sha256}."
    )
