import json
from pathlib import Path

from benchmarks.run_docling_ocr_benchmark import token_recall

ROOT = Path(__file__).resolve().parents[1]


def test_ocr_goldens_have_explicit_text_and_non_native_flags() -> None:
    for fixture_id, page_number in (("scan-en-001", 1), ("mixed-en-001", 2)):
        golden_path = (
            ROOT / "benchmarks" / "fixtures" / "goldens" / f"{fixture_id}.json"
        )
        golden = json.loads(golden_path.read_text())
        page = golden["pages"][page_number - 1]
        assert page["text"]
        assert page["scorable_by_native_parser"] is False
        assert page["scorable_by_ocr"] is True


def test_ocr_token_recall_requires_expected_terms() -> None:
    assert token_recall("scanned archive record", "scanned record") == 2 / 3
    assert token_recall("scanned archive record", "000000") == 0.0
