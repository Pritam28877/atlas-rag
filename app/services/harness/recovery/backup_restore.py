"""Content-addressed backup manifests and fail-closed restore verification."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    ArtifactId,
    SchemaVersion,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
    WorkspaceId,
)

MAXIMUM_BACKUP_COMPONENTS = 256
MAXIMUM_BACKUP_BYTES = 64 * 1024**3
BACKUP_READ_CHUNK_BYTES = 1024 * 1024


class BackupComponentKind(StrEnum):
    DATABASE = "database"
    ARTIFACT = "artifact"
    CONFIGURATION = "configuration"
    PROVENANCE = "provenance"


class BackupComponent(StrictProtocolModel):
    component_id: str = Field(min_length=1, max_length=256)
    kind: BackupComponentKind
    content_sha256: Sha256
    size_bytes: int = Field(ge=1, le=MAXIMUM_BACKUP_BYTES)


class BackupManifest(StrictProtocolModel):
    backup_id: ArtifactId
    workspace_id: WorkspaceId
    schema_version: SchemaVersion
    created_at: UtcTimestamp
    components: tuple[BackupComponent, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_BACKUP_COMPONENTS,
    )
    total_bytes: int = Field(ge=1, le=MAXIMUM_BACKUP_BYTES)
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        component_ids = tuple(component.component_id for component in self.components)
        if tuple(sorted(set(component_ids))) != component_ids:
            raise ValueError("backup component IDs must be unique and sorted")
        component_bytes = sum(
            component.size_bytes for component in self.components
        )
        if component_bytes != self.total_bytes:
            raise ValueError("backup total bytes do not match components")
        if backup_manifest_sha256(self.model_dump(mode="json")) != self.manifest_sha256:
            raise ValueError("backup manifest hash is invalid")
        return self


class RestoreVerification(StrictProtocolModel):
    backup_id: ArtifactId
    accepted: bool
    missing_component_ids: tuple[str, ...] = Field(max_length=MAXIMUM_BACKUP_COMPONENTS)
    mismatched_component_ids: tuple[str, ...] = Field(
        max_length=MAXIMUM_BACKUP_COMPONENTS
    )
    unexpected_component_ids: tuple[str, ...] = Field(
        max_length=MAXIMUM_BACKUP_COMPONENTS
    )
    duplicate_component_ids: tuple[str, ...] = Field(
        max_length=MAXIMUM_BACKUP_COMPONENTS
    )
    reason: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        has_findings = bool(
            self.missing_component_ids
            or self.mismatched_component_ids
            or self.unexpected_component_ids
            or self.duplicate_component_ids
        )
        if self.accepted == has_findings:
            raise ValueError("restore acceptance must agree with findings")
        if not self.accepted and self.reason is None:
            raise ValueError("rejected restore requires a reason")
        return self


def build_backup_manifest(
    *,
    backup_id: ArtifactId,
    workspace_id: WorkspaceId,
    schema_version: SchemaVersion,
    created_at: datetime,
    components: tuple[BackupComponent, ...],
) -> BackupManifest:
    if not components:
        raise ValueError("backup requires at least one component")
    total_bytes = sum(component.size_bytes for component in components)
    provisional = BackupManifest.model_construct(
        backup_id=backup_id,
        workspace_id=workspace_id,
        schema_version=schema_version,
        created_at=created_at,
        components=components,
        total_bytes=total_bytes,
        manifest_sha256="0" * 64,
    )
    return BackupManifest(
        backup_id=backup_id,
        workspace_id=workspace_id,
        schema_version=schema_version,
        created_at=created_at,
        components=components,
        total_bytes=total_bytes,
        manifest_sha256=backup_manifest_sha256(
            provisional.model_dump(mode="json", warnings=False)
        ),
    )


def component_from_path(
    path: Path,
    *,
    component_id: str,
    kind: BackupComponentKind,
) -> BackupComponent:
    """Hash one immutable regular file for a backup manifest.

    Callers should run this blocking operation outside the async request loop.
    The file must remain unchanged while it is read.
    """

    file_path = _require_regular_file(path)
    initial_size = file_path.stat().st_size
    if not 1 <= initial_size <= MAXIMUM_BACKUP_BYTES:
        raise ValueError("backup component size is outside its bound")
    digest = hashlib.sha256()
    total_bytes = 0
    with file_path.open("rb") as file_handle:
        while chunk := file_handle.read(BACKUP_READ_CHUNK_BYTES):
            total_bytes += len(chunk)
            if total_bytes > MAXIMUM_BACKUP_BYTES:
                raise ValueError("backup component exceeds its bound")
            digest.update(chunk)
    if file_path.stat().st_size != initial_size or total_bytes != initial_size:
        raise ValueError("backup component changed while it was hashed")
    return BackupComponent(
        component_id=component_id,
        kind=kind,
        content_sha256=digest.hexdigest(),
        size_bytes=total_bytes,
    )


def snapshot_sqlite_database(
    source_path: Path,
    destination_path: Path,
    *,
    component_id: str,
) -> BackupComponent:
    """Create a consistent SQLite snapshot without overwriting a destination."""

    source = _require_regular_file(source_path)
    destination = _require_new_private_path(destination_path)
    source_connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        source_connection.close()
        destination_connection.close()
    os.chmod(destination, 0o600)
    return component_from_path(
        destination,
        component_id=component_id,
        kind=BackupComponentKind.DATABASE,
    )
def verify_restore(
    manifest: BackupManifest,
    restored_components: tuple[BackupComponent, ...],
) -> RestoreVerification:
    if len(restored_components) > MAXIMUM_BACKUP_COMPONENTS:
        raise ValueError("restored component set exceeds bound")
    expected = {component.component_id: component for component in manifest.components}
    actual: dict[str, BackupComponent] = {}
    duplicate_ids: set[str] = set()
    for component in restored_components:
        if component.component_id in actual:
            duplicate_ids.add(component.component_id)
        actual[component.component_id] = component
    missing = tuple(sorted(set(expected) - set(actual)))
    mismatched = tuple(
        sorted(
            component_id
            for component_id in set(expected) & set(actual)
            if expected[component_id] != actual[component_id]
        )
    )
    unexpected = tuple(sorted(set(actual) - set(expected)))
    duplicates = tuple(sorted(duplicate_ids))
    accepted = not missing and not mismatched and not unexpected and not duplicates
    return RestoreVerification(
        backup_id=manifest.backup_id,
        accepted=accepted,
        missing_component_ids=missing,
        mismatched_component_ids=mismatched,
        unexpected_component_ids=unexpected,
        duplicate_component_ids=duplicates,
        reason=None if accepted else "restored components do not match backup manifest",
    )


def backup_manifest_sha256(values: object) -> str:
    if isinstance(values, dict):
        values = {
            key: value
            for key, value in values.items()
            if key != "manifest_sha256"
        }
    return hashlib.sha256(
        json.dumps(
            values,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _require_regular_file(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("backup paths must be absolute")
    file_path = path.resolve(strict=True)
    file_status = file_path.lstat()
    if not stat.S_ISREG(file_status.st_mode) or path.is_symlink():
        raise ValueError("backup component must be a regular non-symlink file")
    return file_path


def _require_new_private_path(path: Path) -> Path:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise ValueError("backup destination must be a new absolute path")
    parent = path.parent.resolve(strict=True)
    if parent != path.parent:
        raise ValueError("backup destination directory must be canonical")
    parent_status = parent.stat()
    if (
        not stat.S_ISDIR(parent_status.st_mode)
        or parent_status.st_uid != os.getuid()
        or stat.S_IMODE(parent_status.st_mode) & 0o077
    ):
        raise ValueError("backup destination directory must be private")
    return parent / path.name
