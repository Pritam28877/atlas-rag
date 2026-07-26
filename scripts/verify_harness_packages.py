#!/usr/bin/env python3
"""Build, inspect, size-check, and byte-compare Python release archives."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import cast


class PackageVerificationError(ValueError):
    """Raised when a built package is unsafe, oversized, or irreproducible."""


_FORBIDDEN_PARTS = {".git", ".tasks", "__pycache__", "node_modules"}
_FORBIDDEN_FILE_NAMES = {".env", "id_rsa", "id_ed25519"}
_WHEEL_NOTICE = "app/services/harness/NOTICE.md"
_SDIST_NOTICE = "docs/harness/NOTICE.md"


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PackageVerificationError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _positive_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PackageVerificationError(f"{label} must be a positive integer")
    return value


def _validate_archive_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise PackageVerificationError(f"unsafe archive path: {name}")
    if _FORBIDDEN_PARTS.intersection(path.parts):
        raise PackageVerificationError(f"forbidden archive path: {name}")
    if path.name in _FORBIDDEN_FILE_NAMES:
        raise PackageVerificationError(f"secret-bearing archive path: {name}")
    if path.suffix in {".pyc", ".pyo"}:
        raise PackageVerificationError(f"bytecode cannot ship in archive: {name}")
    return path


def _validate_names(names: Iterable[str]) -> set[str]:
    checked: set[str] = set()
    for name in names:
        normalized = _validate_archive_name(name).as_posix()
        if normalized in checked:
            raise PackageVerificationError(f"duplicate archive path: {name}")
        checked.add(normalized)
    return checked


def validate_wheel(path: Path, maximum_bytes: int) -> int:
    size = path.stat().st_size
    if size > maximum_bytes:
        raise PackageVerificationError(
            f"wheel is {size} bytes; budget is {maximum_bytes}"
        )
    try:
        with zipfile.ZipFile(path) as archive:
            names = _validate_names(entry.filename for entry in archive.infolist())
            for entry in archive.infolist():
                mode = entry.external_attr >> 16
                if mode and stat.S_IFMT(mode) == stat.S_IFLNK:
                    raise PackageVerificationError(
                        f"wheel contains symbolic link: {entry.filename}"
                    )
    except zipfile.BadZipFile as error:
        raise PackageVerificationError(f"wheel is invalid: {error}") from error
    if _WHEEL_NOTICE not in names:
        raise PackageVerificationError("wheel omits the harness engineering notice")
    if "app/services/harness/__init__.py" not in names:
        raise PackageVerificationError("wheel omits the harness package")
    return size


def validate_sdist(path: Path, maximum_bytes: int) -> int:
    size = path.stat().st_size
    if size > maximum_bytes:
        raise PackageVerificationError(
            f"sdist is {size} bytes; budget is {maximum_bytes}"
        )
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            members = archive.getmembers()
            names = _validate_names(member.name for member in members)
            for member in members:
                if member.issym() or member.islnk() or member.isdev():
                    raise PackageVerificationError(
                        f"sdist contains unsafe entry: {member.name}"
                    )
    except tarfile.TarError as error:
        raise PackageVerificationError(f"sdist is invalid: {error}") from error
    notice_matches = [
        name for name in names if name.endswith(f"/{_SDIST_NOTICE}")
    ]
    if len(notice_matches) != 1:
        raise PackageVerificationError("sdist must contain exactly one harness notice")
    return size


def _build(root: Path, destination: Path) -> dict[str, Path]:
    completed = subprocess.run(
        ["uv", "build", "--out-dir", str(destination)],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        output = completed.stderr.strip() or completed.stdout.strip()
        raise PackageVerificationError(f"package build failed: {output}")
    files = sorted(
        path
        for path in destination.iterdir()
        if path.name.endswith((".whl", ".tar.gz"))
    )
    if len(files) != 2:
        message = "build must emit exactly one wheel and one sdist"
        raise PackageVerificationError(message)
    artifacts: dict[str, Path] = {}
    for path in files:
        if path.name.endswith(".whl"):
            artifacts["wheel"] = path
        elif path.name.endswith(".tar.gz"):
            artifacts["sdist"] = path
        else:
            raise PackageVerificationError(f"unexpected build artifact: {path.name}")
    if set(artifacts) != {"wheel", "sdist"}:
        raise PackageVerificationError("build did not emit both wheel and sdist")
    return artifacts


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        while chunk := artifact.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_reproducible_packages(root: Path) -> tuple[int, int]:
    policy_path = root / "docs" / "harness" / "supply-chain-policy.json"
    try:
        policy = _mapping(
            json.loads(policy_path.read_text(encoding="utf-8")),
            "supply-chain policy",
        )
    except (OSError, json.JSONDecodeError) as error:
        message = f"cannot read package policy: {error}"
        raise PackageVerificationError(message) from error
    budgets = _mapping(policy.get("artifacts"), "artifact budgets")
    wheel_budget = _positive_integer(budgets.get("wheel_max_bytes"), "wheel budget")
    sdist_budget = _positive_integer(budgets.get("sdist_max_bytes"), "sdist budget")

    with tempfile.TemporaryDirectory(prefix="atlas-harness-package-") as directory:
        temporary_root = Path(directory)
        first = _build(root, temporary_root / "first")
        second = _build(root, temporary_root / "second")
        wheel_size = validate_wheel(first["wheel"], wheel_budget)
        sdist_size = validate_sdist(first["sdist"], sdist_budget)
        for kind in ("wheel", "sdist"):
            if _digest(first[kind]) != _digest(second[kind]):
                raise PackageVerificationError(f"{kind} build is not reproducible")
    return wheel_size, sdist_size


def main() -> int:
    try:
        wheel_size, sdist_size = verify_reproducible_packages(Path.cwd())
    except (
        OSError,
        PackageVerificationError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"Package verification failed: {error}", file=sys.stderr)
        return 1
    print(
        f"Packages valid and reproducible: wheel {wheel_size} bytes, "
        f"sdist {sdist_size} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
