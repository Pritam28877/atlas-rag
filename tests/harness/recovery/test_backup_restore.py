"""Backup manifest and restore verification tests."""

import sqlite3
from datetime import UTC, datetime

import pytest

from app.services.harness.recovery import (
    BackupComponent,
    BackupComponentKind,
    build_backup_manifest,
    component_from_path,
    snapshot_sqlite_database,
    verify_restore,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _component(identifier: str, digest: str) -> BackupComponent:
    return BackupComponent(
        component_id=identifier,
        kind=BackupComponentKind.ARTIFACT,
        content_sha256=digest,
        size_bytes=4,
    )


def test_restore_accepts_exact_content_addressed_components() -> None:
    manifest = build_backup_manifest(
        backup_id="art_" + "1" * 32,
        workspace_id="wsp_" + "2" * 32,
        schema_version="1.2",
        created_at=NOW,
        components=(_component("artifact-a", "a" * 64),),
    )
    verification = verify_restore(manifest, manifest.components)
    assert verification.accepted


def test_restore_rejects_missing_or_mismatched_components() -> None:
    manifest = build_backup_manifest(
        backup_id="art_" + "1" * 32,
        workspace_id="wsp_" + "2" * 32,
        schema_version="1.2",
        created_at=NOW,
        components=(_component("artifact-a", "a" * 64),),
    )
    verification = verify_restore(manifest, (_component("artifact-a", "b" * 64),))
    assert not verification.accepted
    assert verification.mismatched_component_ids == ("artifact-a",)
    assert verification.unexpected_component_ids == ()
    assert verification.duplicate_component_ids == ()
    with pytest.raises(ValueError, match="acceptance"):
        type(verification).model_validate(
            {
                **verification.model_dump(),
                "accepted": True,
            }
        )


def test_restore_rejects_unexpected_and_duplicate_components() -> None:
    manifest = build_backup_manifest(
        backup_id="art_" + "1" * 32,
        workspace_id="wsp_" + "2" * 32,
        schema_version="1.2",
        created_at=NOW,
        components=(_component("artifact-a", "a" * 64),),
    )
    extra = _component("artifact-b", "b" * 64)
    verification = verify_restore(
        manifest,
        (manifest.components[0], extra, extra),
    )
    assert not verification.accepted
    assert verification.unexpected_component_ids == ("artifact-b",)
    assert verification.duplicate_component_ids == ("artifact-b",)


def test_sqlite_snapshot_is_consistent_and_content_addressed(tmp_path) -> None:
    source_path = tmp_path / "source.sqlite"
    destination_path = tmp_path / "backup.sqlite"
    with sqlite3.connect(source_path) as connection:
        connection.execute("CREATE TABLE values_table (value TEXT NOT NULL)")
        connection.execute("INSERT INTO values_table VALUES ('stable')")
        connection.commit()

    component = snapshot_sqlite_database(
        source_path,
        destination_path,
        component_id="database",
    )
    with sqlite3.connect(destination_path) as connection:
        row = connection.execute(
            "SELECT value FROM values_table"
        ).fetchone()
    assert row == ("stable",)
    assert component == component_from_path(
        destination_path,
        component_id="database",
        kind=BackupComponentKind.DATABASE,
    )
