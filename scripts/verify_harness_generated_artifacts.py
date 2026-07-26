#!/usr/bin/env python3
"""Verify generated Atlas Harness artifacts against their checked-in manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import cast


class GeneratedArtifactError(ValueError):
    """Raised when generated output is missing, unsafe, or hand edited."""


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GeneratedArtifactError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise GeneratedArtifactError(f"{label} keys must be strings")
    return cast(dict[str, object], value)


def _required_string(entry: Mapping[str, object], field: str, label: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise GeneratedArtifactError(f"{label}.{field} must be a non-empty string")
    return value


def _safe_relative_path(value: str, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise GeneratedArtifactError(f"{label} must stay inside the repository")
    if not path.parts:
        raise GeneratedArtifactError(f"{label} cannot be empty")
    return path


def validate_generated_artifacts(
    root: Path,
    manifest: Mapping[str, object],
) -> int:
    if manifest.get("schema_version") != 1:
        raise GeneratedArtifactError("generated manifest schema_version must be 1")
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise GeneratedArtifactError("generated manifest artifacts must be an array")

    seen_paths: set[str] = set()
    for index, raw_artifact in enumerate(raw_artifacts):
        label = f"artifacts[{index}]"
        artifact = _mapping(raw_artifact, label)
        relative_text = _required_string(artifact, "path", label)
        relative_path = _safe_relative_path(relative_text, f"{label}.path")
        if relative_text in seen_paths:
            raise GeneratedArtifactError(f"duplicate generated path: {relative_text}")
        seen_paths.add(relative_text)

        generator = _required_string(artifact, "generator", label)
        marker = _required_string(artifact, "marker", label)
        expected_digest = _required_string(artifact, "sha256", label)
        if len(generator) < 3:
            raise GeneratedArtifactError(f"{label}.generator is too short")
        if len(expected_digest) != 64:
            raise GeneratedArtifactError(f"{label}.sha256 must be a SHA-256 digest")

        artifact_path = root.joinpath(*relative_path.parts)
        if not artifact_path.is_file() or artifact_path.is_symlink():
            raise GeneratedArtifactError(
                f"generated artifact must be a regular file: {relative_text}"
            )
        content = artifact_path.read_bytes()
        if len(content) > 16 * 1024 * 1024:
            raise GeneratedArtifactError(
                f"generated artifact exceeds 16 MiB: {relative_text}"
            )
        header = b"\n".join(content.splitlines()[:5])
        if marker.encode("utf-8") not in header:
            raise GeneratedArtifactError(
                f"generated marker is missing from {relative_text}"
            )
        actual_digest = hashlib.sha256(content).hexdigest()
        if actual_digest != expected_digest:
            raise GeneratedArtifactError(
                f"generated artifact digest changed: {relative_text}"
            )
    return len(raw_artifacts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("docs/harness/generated-artifacts.json"),
    )
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    manifest_path = arguments.manifest
    if not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    try:
        manifest = _mapping(
            json.loads(manifest_path.read_text(encoding="utf-8")),
            str(manifest_path),
        )
        artifact_count = validate_generated_artifacts(root, manifest)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        GeneratedArtifactError,
    ) as error:
        print(f"Generated-artifact verification failed: {error}", file=sys.stderr)
        return 1
    print(f"Generated-artifact manifest valid: {artifact_count} artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
