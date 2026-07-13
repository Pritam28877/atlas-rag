"""Benchmark local Tesseract OCR against hash-verified PDF fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import resource
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from run_docling_ocr_benchmark import (
    MAX_TRIALS,
    ROOT,
    ocr_expectations,
    score_records,
    summarize,
)
from validate_fixture_manifest import load_valid_manifest

RESULTS_PATH = ROOT / "results" / "tesseract-ocr.json"
RENDER_DPI = 300
OCR_LANGUAGE = "eng"
OCR_PAGE_SEGMENTATION_MODE = "3"


def command_path(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"Required OCR command is unavailable: {name}")
    return path


def command_output(*arguments: str) -> str:
    return subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def tessdata_manifest(tesseract: str) -> dict[str, Any]:
    languages = command_output(tesseract, "--list-langs")
    match = re.search(r'List of available languages in "(.+)"', languages)
    if match is None:
        raise RuntimeError("Tesseract did not report its tessdata directory")
    traineddata = Path(match.group(1)) / f"{OCR_LANGUAGE}.traineddata"
    if not traineddata.is_file():
        raise RuntimeError(f"Missing Tesseract language pack: {traineddata}")
    return {
        "binary_version": command_output(tesseract, "--version").splitlines()[0],
        "language": OCR_LANGUAGE,
        "language_pack": {
            "path": str(traineddata),
            "bytes": traineddata.stat().st_size,
            "sha256": hashlib.sha256(traineddata.read_bytes()).hexdigest(),
        },
        "render_dpi": RENDER_DPI,
        "page_segmentation_mode": OCR_PAGE_SEGMENTATION_MODE,
    }


def rendered_pages(pdftoppm: str, source: Path, directory: Path) -> list[Path]:
    prefix = directory / "page"
    subprocess.run(
        [pdftoppm, "-r", str(RENDER_DPI), "-png", str(source), str(prefix)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    pages = sorted(directory.glob("page-*.png"))
    if not pages:
        raise RuntimeError(f"PDF renderer produced no pages: {source}")
    return pages


def page_text(tesseract: str, page: Path) -> str:
    completed = subprocess.run(
        [
            tesseract,
            str(page),
            "stdout",
            "-l",
            OCR_LANGUAGE,
            "--psm",
            OCR_PAGE_SEGMENTATION_MODE,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return completed.stdout.strip()


def records_for_paths(
    tesseract: str, pdftoppm: str, paths: list[Path], repeats: int
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for _ in range(repeats):
        for source in paths:
            started = time.perf_counter()
            with tempfile.TemporaryDirectory(prefix="tesseract-ocr-") as temporary:
                pages = rendered_pages(pdftoppm, source, Path(temporary))
                text = {
                    str(index): page_text(tesseract, page)
                    for index, page in enumerate(pages, start=1)
                }
            records.append(
                {
                    "fixture_id": source.stem,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "status": "success",
                    "pages": text,
                    "peak_rss_kib": resource.getrusage(
                        resource.RUSAGE_CHILDREN
                    ).ru_maxrss,
                }
            )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=MAX_TRIALS)
    args = parser.parse_args()
    if not 1 <= args.trials <= MAX_TRIALS:
        raise SystemExit(f"--trials must be between 1 and {MAX_TRIALS}")

    tesseract = command_path("tesseract")
    pdftoppm = command_path("pdftoppm")
    manifest = load_valid_manifest()
    expectations = ocr_expectations(manifest)
    paths = [ROOT / "fixtures" / f"{fixture_id}.pdf" for fixture_id in expectations]
    cold = score_records(
        records_for_paths(tesseract, pdftoppm, paths, args.trials), expectations
    )
    warm = score_records(
        records_for_paths(tesseract, pdftoppm, paths, args.trials), expectations
    )
    results = {
        "tesseract": {
            "cold": summarize(cold),
            "warm": summarize(warm),
        },
        "provenance": tessdata_manifest(tesseract),
    }
    RESULTS_PATH.parent.mkdir(exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
