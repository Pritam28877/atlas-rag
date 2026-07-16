"""Document catalog domain types and state-transition rules."""

from app.services.catalog.enums import (
    ArtifactType,
    CollectionRole,
    IngestionStage,
    JobState,
    PublicationKind,
    VersionState,
)
from app.services.catalog.state_machine import (
    IllegalTransitionError,
    PublicationEvidence,
    StaleTransitionError,
    VersionSnapshot,
    transition_version,
)

__all__ = [
    "ArtifactType",
    "CollectionRole",
    "IllegalTransitionError",
    "IngestionStage",
    "JobState",
    "PublicationEvidence",
    "PublicationKind",
    "StaleTransitionError",
    "VersionSnapshot",
    "VersionState",
    "transition_version",
]
