import copy
import json
import subprocess
import sys
from pathlib import Path

from scripts.verify_harness_provenance import ManifestError, validate_manifest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docs" / "harness" / "provenance-manifest.json"


def load_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_checked_in_manifest_passes_real_cli() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/verify_harness_provenance.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "Provenance manifest valid: 6 sources, 0 adapted files"
    )


def test_unlicensed_source_cannot_allow_copying() -> None:
    manifest = load_manifest()
    source = copy.deepcopy(manifest["sources"][-1])
    source["usage"]["copy_allowed"] = True
    source["usage"]["treatment"] = "pattern_adaptation"
    manifest["sources"][-1] = source

    try:
        validate_manifest(manifest)
    except ManifestError as error:
        assert "verified permissive license" in str(error)
    else:
        raise AssertionError("unlicensed source unexpectedly allowed copying")


def test_source_requires_full_immutable_git_revision() -> None:
    manifest = load_manifest()
    manifest["sources"][0]["revision"] = "4c43465"

    try:
        validate_manifest(manifest)
    except ManifestError as error:
        assert "full lowercase Git commit" in str(error)
    else:
        raise AssertionError("short Git revision unexpectedly passed")


def test_duplicate_source_id_is_rejected() -> None:
    manifest = load_manifest()
    duplicate = copy.deepcopy(manifest["sources"][0])
    manifest["sources"].append(duplicate)

    try:
        validate_manifest(manifest)
    except ManifestError as error:
        assert "duplicate source id" in str(error)
    else:
        raise AssertionError("duplicate source ID unexpectedly passed")


def test_adapted_file_cannot_reference_non_copy_source() -> None:
    manifest = load_manifest()
    source = manifest["sources"][-1]
    manifest["adapted_files"].append(
        {
            "path": "atlas-harness/crates/example/src/lib.rs",
            "source_id": source["id"],
            "source_path": "server/example.py",
            "source_revision": source["revision"],
            "license": "Apache-2.0",
            "change_summary": "Example forbidden adaptation.",
            "owner": "Atlas maintainer",
        }
    )

    try:
        validate_manifest(manifest)
    except ManifestError as error:
        assert "forbids copying" in str(error)
    else:
        raise AssertionError("forbidden adapted file unexpectedly passed")


def test_adapted_file_path_cannot_escape_repository() -> None:
    manifest = load_manifest()
    source = manifest["sources"][0]
    manifest["adapted_files"].append(
        {
            "path": "../outside.rs",
            "source_id": source["id"],
            "source_path": "codex-rs/example/src/lib.rs",
            "source_revision": source["revision"],
            "license": source["license"]["spdx"],
            "change_summary": "Example invalid destination.",
            "owner": "Atlas maintainer",
        }
    )

    try:
        validate_manifest(manifest)
    except ManifestError as error:
        assert "stay inside the repository" in str(error)
    else:
        raise AssertionError("escaping adapted path unexpectedly passed")
