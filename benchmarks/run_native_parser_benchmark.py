"""Validate native PDF routing against the hash-verified fixture manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from validate_fixture_manifest import load_valid_manifest

ROOT = Path(__file__).resolve().parent
RESULTS_PATH = ROOT / "results" / "native-parser-pypdf.json"
logging.getLogger("pypdf").setLevel(logging.ERROR)


def normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


def sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def resolve(value: Any) -> Any:
    return value.get_object() if hasattr(value, "get_object") else value


def has_active_content(reader: PdfReader) -> bool:
    root = resolve(reader.trailer["/Root"])
    actions = [root.get("/OpenAction")]
    names = resolve(root.get("/Names"))
    if names and names.get("/EmbeddedFiles"):
        return True
    for page in reader.pages:
        page_object = resolve(page)
        actions.extend([page_object.get("/AA"), page_object.get("/OpenAction")])
    for action in actions:
        action = resolve(action)
        if not action:
            continue
        subtype = action.get("/S")
        if str(subtype) in {"/JavaScript", "/URI", "/Launch", "/SubmitForm"}:
            return True
    return False


def expected_golden(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    reference = fixture["expected_outcome"]["golden_ref"]
    if reference is None:
        return []
    return json.loads((ROOT.parent / reference).read_text(encoding="utf-8"))["pages"]


def terminal_state(route: str) -> str:
    return {
        "reject": "REJECTED",
        "quarantine": "QUARANTINED",
        "deduplicate": "DEDUPLICATED",
    }.get(route, "READY")


def inspect_fixture(
    fixture: dict[str, Any], limit_profile_max_bytes: int
) -> dict[str, Any]:
    artifact = fixture["artifact"]
    path = ROOT.parent / artifact["object_ref"]
    expected = fixture["expected_outcome"]
    result: dict[str, Any] = {
        "fixture_id": fixture["id"],
        "bytes": path.stat().st_size,
        "expected": {
            "route": expected["route"],
            "terminal_state": expected["terminal_state"],
            "reason_code": expected["reason_code"],
        },
    }
    if (
        fixture["resource_profile"] == "limit"
        and result["bytes"] > limit_profile_max_bytes
    ):
        observed = {
            "route": "reject",
            "terminal_state": "REJECTED",
            "reason_code": "UPLOAD_SIZE_EXCEEDED",
        }
    elif fixture.get("duplicate_of"):
        source = fixture["duplicate_of"]
        manifest = load_valid_manifest()
        source_fixture = next(
            item for item in manifest["fixtures"] if item["id"] == source
        )
        source_path = ROOT.parent / source_fixture["artifact"]["object_ref"]
        observed = (
            {
                "route": "deduplicate",
                "terminal_state": "DEDUPLICATED",
                "reason_code": "DUPLICATE_CONTENT",
            }
            if sha256(path) == sha256(source_path)
            else {
                "route": "native_parse",
                "terminal_state": "READY",
                "reason_code": None,
            }
        )
    else:
        started = time.perf_counter()
        try:
            reader = PdfReader(path)
            if reader.is_encrypted:
                observed = {
                    "route": "reject",
                    "terminal_state": "REJECTED",
                    "reason_code": "PDF_ENCRYPTED_UNSUPPORTED",
                }
            elif has_active_content(reader):
                observed = {
                    "route": "quarantine",
                    "terminal_state": "QUARANTINED",
                    "reason_code": "ACTIVE_CONTENT_DETECTED",
                }
            else:
                pages = [(page.extract_text() or "").strip() for page in reader.pages]
                text_pages = sum(bool(page) for page in pages)
                route = (
                    "ocr"
                    if text_pages == 0
                    else "mixed"
                    if text_pages < len(pages)
                    else "native_parse"
                )
                observed = {
                    "route": route,
                    "terminal_state": terminal_state(route),
                    "reason_code": None,
                    "page_count": len(pages),
                    "citation_coverage": text_pages / len(pages),
                    "pages": pages,
                }
        except Exception as error:
            observed = {
                "route": "reject",
                "terminal_state": "FAILED",
                "reason_code": "PDF_MALFORMED",
                "error_type": type(error).__name__,
            }
        result["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)

    result["observed"] = observed
    route_matches = observed["route"] == expected["route"]
    state_matches = observed["terminal_state"] == expected["terminal_state"]
    reason_matches = observed["reason_code"] == expected["reason_code"]
    golden_matches = True
    if observed.get("pages"):
        golden = expected_golden(fixture)
        golden_matches = all(
            not page["scorable_by_native_parser"]
            or normalize(page["text"]) in normalize(observed["pages"][index])
            for index, page in enumerate(golden)
        )
    result["status"] = (
        "passed"
        if all([route_matches, state_matches, reason_matches, golden_matches])
        else "failed"
    )
    result["golden_matches"] = golden_matches
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--limit-profile-max-bytes",
        type=int,
        default=1_024,
        help="Limit used only for manifest fixtures with resource_profile=limit.",
    )
    args = parser.parse_args()
    manifest = load_valid_manifest()
    records = [
        inspect_fixture(fixture, args.limit_profile_max_bytes)
        for fixture in manifest["fixtures"]
    ]
    passed = sum(record["status"] == "passed" for record in records)
    payload = {
        "candidate": {
            "component": "native_parser",
            "name": "pypdf",
            "version": "6.14.2",
        },
        "python": sys.version,
        "limit_profile_max_bytes": args.limit_profile_max_bytes,
        "summary": {
            "total": len(records),
            "passed": passed,
            "failed": len(records) - passed,
        },
        "records": records,
    }
    RESULTS_PATH.parent.mkdir(exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if passed != len(records):
        raise SystemExit("Native parser benchmark has failing fixtures.")


if __name__ == "__main__":
    main()
