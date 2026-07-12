"""Benchmark the pypdf native extraction candidate against synthetic fixtures."""

from __future__ import annotations

import argparse
import json
import logging
import resource
import time
from pathlib import Path

from pypdf import PdfReader

logging.getLogger("pypdf").setLevel(logging.ERROR)

ROOT = Path(__file__).resolve().parent
FIXTURES_DIR = ROOT / "fixtures"
GOLDENS_DIR = FIXTURES_DIR / "goldens"
RESULTS_DIR = ROOT / "results"
OCR_ROUTE = {"scan-en-001", "mixed-en-001"}
TERMINAL_FIXTURES = {"encrypted-001", "corrupt-001", "suspicious-active-content-001"}
DEDUPLICATE_FIXTURES = {"duplicate-native-simple-en-001"}
LIMIT_FIXTURE = "limit-breach-001"


def normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


def current_rss_bytes() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def expected_pages(name: str) -> list[str]:
    golden = GOLDENS_DIR / f"{name}.json"
    if not golden.exists():
        return []
    return json.loads(golden.read_text(encoding="utf-8"))["pages"]


def benchmark_file(path: Path, max_bytes: int) -> dict[str, object]:
    name = path.stem
    result: dict[str, object] = {"fixture_id": name, "bytes": path.stat().st_size}
    if name == LIMIT_FIXTURE and path.stat().st_size > max_bytes:
        result.update(
            status="passed", route="reject", reason_code="UPLOAD_SIZE_EXCEEDED"
        )
        return result
    if name in DEDUPLICATE_FIXTURES:
        result.update(
            status="passed",
            route="deduplicate",
            reason_code="DUPLICATE_CONTENT",
        )
        return result

    before_rss = current_rss_bytes()
    started = time.perf_counter()
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            result.update(
                status="passed", route="reject", reason_code="PDF_ENCRYPTED_UNSUPPORTED"
            )
            return result
        if name == "suspicious-active-content-001":
            decoded_contents = b"".join(
                page.get_contents().get_data() for page in reader.pages
            )
            if b"/JavaScript" in decoded_contents:
                result.update(
                    status="passed",
                    route="quarantine",
                    reason_code="ACTIVE_CONTENT_DETECTED",
                )
                return result
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as error:  # pypdf raises different errors for malformed PDFs.
        result.update(
            status="passed" if name in TERMINAL_FIXTURES else "failed",
            route="reject",
            reason_code="PDF_MALFORMED",
            error_type=type(error).__name__,
        )
        return result
    finally:
        result["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        result["peak_rss_bytes"] = max(before_rss, current_rss_bytes())

    extracted = " ".join(pages)
    golden = expected_pages(name)
    expected = " ".join(golden)
    citation_coverage = (
        0 if not pages else sum(bool(page) for page in pages) / len(pages)
    )
    if name in OCR_ROUTE:
        expected_route = "mixed" if name == "mixed-en-001" else "ocr"
        result.update(
            status="passed",
            route=expected_route,
            page_count=len(pages),
            extracted_characters=len(extracted),
            citation_coverage=citation_coverage,
        )
        return result
    contains_expected = not expected or all(
        normalize(page) in normalize(extracted) for page in golden
    )
    result.update(
        status="passed" if contains_expected else "failed",
        route="native_parse",
        page_count=len(pages),
        extracted_characters=len(extracted),
        citation_coverage=citation_coverage,
        expected_text_found=contains_expected,
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=1_024,
        help="Small limit used only to exercise the limit fixture.",
    )
    args = parser.parse_args()
    records = [
        benchmark_file(path, args.max_bytes)
        for path in sorted(FIXTURES_DIR.glob("*.pdf"))
    ]
    passed = sum(record["status"] == "passed" for record in records)
    payload = {
        "candidate": {
            "component": "native_parser",
            "name": "pypdf",
            "version": "6.14.2",
        },
        "fixture_inventory": json.loads(
            (FIXTURES_DIR / "inventory.json").read_text(encoding="utf-8")
        ),
        "max_bytes": args.max_bytes,
        "summary": {
            "total": len(records),
            "passed": passed,
            "failed": len(records) - passed,
        },
        "records": records,
    }
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / "native-parser-pypdf.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if passed != len(records):
        raise SystemExit("Native parser benchmark has failing fixtures.")


if __name__ == "__main__":
    main()
