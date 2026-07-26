"""Bound and deterministically order online projection definitions."""

from collections.abc import Sequence
from typing import Any

from app.services.harness.journal.projection_contracts import ProjectionDefinition

MAXIMUM_ONLINE_PROJECTIONS = 32


def prepare_online_projections(
    definitions: Sequence[ProjectionDefinition[Any]],
) -> tuple[ProjectionDefinition[Any], ...]:
    if len(definitions) > MAXIMUM_ONLINE_PROJECTIONS:
        raise ValueError("online projection count exceeds 32")
    ordered = tuple(sorted(definitions, key=lambda definition: definition.name))
    names = tuple(definition.name for definition in ordered)
    if len(names) != len(set(names)):
        raise ValueError("online projection names must be unique")
    return ordered
