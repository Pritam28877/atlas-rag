"""Benchmark offline Docling OCR engines against hash-verified fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    from .validate_fixture_manifest import load_valid_manifest
except ImportError:  # Direct script execution keeps the benchmark standalone.
    from validate_fixture_manifest import load_valid_manifest

ROOT = Path(__file__).resolve().parent
ARTIFACTS_PATH = ROOT / "ocr-artifacts"
RESULTS_PATH = ROOT / "results" / "docling-ocr.json"
MAX_TRIALS = 5
WORKER_FILE_BYTES = 100 * 1024**2
WORKER_TIMEOUT_SECONDS = 120


def normalize(text: str) -> list[str]:
    normalized = "".join(char.lower() if char.isalnum() else " " for char in text)
    return normalized.split()


def token_recall(expected: str, observed: str) -> float:
    expected_tokens = set(normalize(expected))
    if not expected_tokens:
        return 1.0
    return len(expected_tokens & set(normalize(observed))) / len(expected_tokens)


def artifact_manifest() -> dict[str, Any]:
    files = sorted(path for path in ARTIFACTS_PATH.rglob("*") if path.is_file())
    return {
        "file_count": len(files),
        "bytes": sum(path.stat().st_size for path in files),
        "sha256": hashlib.sha256(
            "".join(
                f"{path.relative_to(ARTIFACTS_PATH)}:{hashlib.sha256(path.read_bytes()).hexdigest()}\n"
                for path in files
            ).encode()
        ).hexdigest(),
    }


def candidate_options(engine: str) -> Any:
    from docling.datamodel.pipeline_options import EasyOcrOptions, RapidOcrOptions

    if engine == "rapidocr-torch":
        return RapidOcrOptions(lang=["eng"], backend="torch", force_full_page_ocr=True)
    if engine == "rapidocr-onnx":
        return RapidOcrOptions(
            lang=["eng"], backend="onnxruntime", force_full_page_ocr=True
        )
    if engine == "easyocr":
        return EasyOcrOptions(
            lang=["en"], force_full_page_ocr=True, confidence_threshold=0.1
        )
    raise ValueError(f"Unknown OCR engine: {engine}")


def worker_convert(
    engine: str, paths: list[Path], repeats: int
) -> list[dict[str, Any]]:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(
        artifacts_path=ARTIFACTS_PATH,
        do_ocr=True,
        do_table_structure=False,
        do_formula_enrichment=False,
        do_code_enrichment=False,
        do_picture_classification=False,
        do_picture_description=False,
        ocr_options=candidate_options(engine),
    )
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
    records: list[dict[str, Any]] = []
    for _ in range(repeats):
        for path in paths:
            started = time.perf_counter()
            result = converter.convert(path)
            texts: dict[int, list[str]] = defaultdict(list)
            for item in result.document.export_to_dict().get("texts", []):
                for provenance in item.get("prov", []):
                    texts[provenance["page_no"]].append(item["text"])
            records.append(
                {
                    "fixture_id": path.stem,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "status": str(result.status),
                    "pages": {
                        str(page): " ".join(text) for page, text in texts.items()
                    },
                    "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                }
            )
    return records


def resource_limits() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (WORKER_FILE_BYTES, WORKER_FILE_BYTES))


def invoke_worker(engine: str, paths: list[Path], repeats: int) -> list[dict[str, Any]]:
    environment = {
        "HF_HUB_OFFLINE": "1",
        "DOCLING_DEVICE": "cpu",
        "DOCLING_NUM_THREADS": "2",
        "OMP_NUM_THREADS": "2",
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--engine",
            engine,
            "--repeats",
            str(repeats),
            *map(str, paths),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=WORKER_TIMEOUT_SECONDS,
        env={**os.environ, **environment},
        preexec_fn=resource_limits,
    )
    if completed.returncode:
        raise RuntimeError(
            f"OCR worker failed for {engine}: {completed.stderr[-2_000:]}"
        )
    marker = "RESULT_JSON:"
    payload = next(
        (
            line.removeprefix(marker)
            for line in reversed(completed.stdout.splitlines())
            if line.startswith(marker)
        ),
        None,
    )
    if payload is None:
        raise RuntimeError("OCR worker did not emit a result payload")
    return json.loads(payload)


def ocr_expectations(manifest: dict[str, Any]) -> dict[str, dict[int, str]]:
    expected: dict[str, dict[int, str]] = {}
    for fixture in manifest["fixtures"]:
        golden_ref = fixture["expected_outcome"]["golden_ref"]
        if not golden_ref:
            continue
        golden = json.loads((ROOT.parent / golden_ref).read_text(encoding="utf-8"))
        pages = {
            page["page_number"]: page["text"]
            for page in golden["pages"]
            if page.get("scorable_by_ocr")
        }
        if pages:
            expected[fixture["id"]] = pages
    return expected


def score_records(
    records: list[dict[str, Any]], expectations: dict[str, dict[int, str]]
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for record in records:
        pages = expectations[record["fixture_id"]]
        recall_scores = [
            token_recall(expected, record["pages"].get(str(page_number), ""))
            for page_number, expected in pages.items()
        ]
        scored.append(
            {
                **record,
                "token_recall": statistics.mean(recall_scores),
                "page_citation_coverage": sum(
                    str(page_number) in record["pages"] for page_number in pages
                )
                / len(pages),
            }
        )
    return scored


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "runs": len(records),
        "p50_duration_ms": statistics.median(
            record["duration_ms"] for record in records
        ),
        "p95_duration_ms": sorted(record["duration_ms"] for record in records)[-1],
        "min_token_recall": min(record["token_recall"] for record in records),
        "min_page_citation_coverage": min(
            record["page_citation_coverage"] for record in records
        ),
        "max_peak_rss_kib": max(record["peak_rss_kib"] for record in records),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument(
        "--engine", choices=["rapidocr-torch", "rapidocr-onnx", "easyocr"]
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--trials", type=int, default=MAX_TRIALS)
    parser.add_argument(
        "--engines",
        nargs="+",
        choices=["rapidocr-torch", "rapidocr-onnx", "easyocr"],
    )
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()
    if args.worker:
        print(
            "RESULT_JSON:"
            + json.dumps(
                worker_convert(
                    args.engine, [Path(path) for path in args.paths], args.repeats
                )
            )
        )
        return
    if not ARTIFACTS_PATH.is_dir():
        raise SystemExit("Pre-fetch Docling artifacts before running the OCR benchmark")
    if not 1 <= args.trials <= MAX_TRIALS:
        raise SystemExit(f"--trials must be between 1 and {MAX_TRIALS}")
    manifest = load_valid_manifest()
    expectations = ocr_expectations(manifest)
    paths = [ROOT / "fixtures" / f"{fixture_id}.pdf" for fixture_id in expectations]
    results: dict[str, Any] = {}
    for engine in args.engines or ("rapidocr-torch", "rapidocr-onnx", "easyocr"):
        cold_records = [
            record
            for _ in range(args.trials)
            for record in invoke_worker(engine, paths, 1)
        ]
        cold = score_records(cold_records, expectations)
        warm = score_records(invoke_worker(engine, paths, args.trials), expectations)
        results[engine] = {"cold": summarize(cold), "warm": summarize(warm)}
    RESULTS_PATH.parent.mkdir(exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps({"artifacts": artifact_manifest(), "candidates": results}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
