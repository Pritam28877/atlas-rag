import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = ROOT / "benchmarks" / "fixtures"
RESULT_PATH = ROOT / "benchmarks" / "results" / "native-parser-pypdf.json"


def run_script(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, f"benchmarks/{script}", *arguments],
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_manifest_validator_rejects_unmanifested_pdf() -> None:
    unmanifested = FIXTURES_DIR / "unmanifested-test.pdf"
    unmanifested.write_bytes(b"%PDF-1.7\n")
    try:
        result = run_script("validate_fixture_manifest.py")
    finally:
        unmanifested.unlink(missing_ok=True)

    assert result.returncode != 0
    assert "Manifest fixture paths do not match" in result.stderr


def test_native_parser_benchmark_matches_manifest() -> None:
    result = run_script(
        "run_native_parser_benchmark.py", "--limit-profile-max-bytes", "1024"
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    assert payload["summary"] == {"total": 12, "passed": 12, "failed": 0}
    for record in payload["records"]:
        assert record["status"] == "passed"
        observed = record["observed"]
        assert {
            "route": observed["route"],
            "terminal_state": observed["terminal_state"],
            "reason_code": observed["reason_code"],
        } == record["expected"]


def test_tesseract_benchmark_exposes_its_cli() -> None:
    result = run_script("run_tesseract_ocr_benchmark.py", "--help")

    assert result.returncode == 0, result.stderr
    assert "Benchmark local Tesseract OCR" in result.stdout
