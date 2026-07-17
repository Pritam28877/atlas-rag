import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from app.core.config import IngestionPolicySettings
from app.services.catalog.enums import VersionState
from app.services.ingestion.artifacts import (
    PageArtifactWriter,
    iter_page_artifact,
    merge_ocr_pages,
)
from app.services.ingestion.errors import TerminalIngestionError
from app.services.ingestion.models import PageOrigin
from app.services.ingestion.ocr_provider import (
    OcrProviderTemporaryError,
    TesseractOcrProvider,
    _parse_tsv,
    _run_command,
)
from app.services.ingestion.parser import PypdfNativeParser

FIXTURES = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures"
VERSION_ID = UUID("1dd21a4f-c0dc-45b4-8e36-5b9c9a7012a9")


def test_tesseract_enriches_only_selected_scan_page(tmp_path: Path) -> None:
    provider = TesseractOcrProvider()

    pages = provider.enrich_pages(
        FIXTURES / "mixed-en-001.pdf",
        [2],
        IngestionPolicySettings(),
        tmp_path,
    )

    assert len(pages) == 1
    assert pages[0].page_number == 2
    assert pages[0].origin is PageOrigin.OCR
    assert "SCANNED ARCHIVE RECORD" in pages[0].text
    assert pages[0].quality_score >= 0.70
    assert pages[0].blocks
    assert list(tmp_path.iterdir()) == []


def test_ocr_merge_preserves_native_and_ocr_page_provenance(
    tmp_path: Path,
) -> None:
    policy = IngestionPolicySettings()
    parser = PypdfNativeParser()
    extracted_path = tmp_path / "extracted.jsonl"
    writer = PageArtifactWriter(
        extracted_path,
        VERSION_ID,
        parser.name,
        parser.parser_version,
    )
    try:
        inspection = parser.parse(
            FIXTURES / "mixed-en-001.pdf",
            policy,
            writer.write_page,
        )
        writer.finish(inspection)
    finally:
        writer.close()
    provider = TesseractOcrProvider()
    ocr_pages = provider.enrich_pages(
        FIXTURES / "mixed-en-001.pdf",
        inspection.ocr_page_numbers,
        policy,
        tmp_path,
    )
    normalized_path = tmp_path / "normalized.jsonl"

    merged_inspection = merge_ocr_pages(
        extracted_path,
        normalized_path,
        VERSION_ID,
        ocr_pages,
        "pypdf+tesseract-test",
    )
    merged_pages = list(iter_page_artifact(normalized_path, VERSION_ID))

    assert merged_inspection.page_count == 2
    assert [page.origin for page in merged_pages] == [
        PageOrigin.NATIVE,
        PageOrigin.OCR,
    ]
    assert "native page" in merged_pages[0].text
    assert "pixels only" in merged_pages[1].text


@pytest.mark.parametrize(
    ("rows", "reason_code"),
    [
        ([], "OCR_NO_TEXT_DETECTED"),
        (
            [
                "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t20\tuncertain",
            ],
            "OCR_QUALITY_BELOW_THRESHOLD",
        ),
    ],
)
def test_ocr_quality_failures_are_explicit(
    tmp_path: Path,
    rows: list[str],
    reason_code: str,
) -> None:
    tsv_path = tmp_path / "result.tsv"
    header = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext"
    )
    tsv_path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")

    with pytest.raises(TerminalIngestionError) as captured:
        _parse_tsv(tsv_path, 1, IngestionPolicySettings())

    assert captured.value.state is VersionState.FAILED
    assert captured.value.reason_code == reason_code


def test_unsupported_ocr_language_has_safe_terminal_outcome(tmp_path: Path) -> None:
    provider = TesseractOcrProvider()

    with pytest.raises(TerminalIngestionError) as captured:
        provider.enrich_pages(
            FIXTURES / "scan-en-001.pdf",
            [1],
            IngestionPolicySettings(ocr_language_codes=("not-installed",)),
            tmp_path,
        )

    assert captured.value.reason_code == "OCR_LANGUAGE_UNSUPPORTED"


@pytest.mark.parametrize(
    ("raised_error", "expected_error"),
    [
        (
            subprocess.TimeoutExpired(["ocr"], 1),
            TerminalIngestionError,
        ),
        (
            subprocess.CalledProcessError(1, ["ocr"]),
            OcrProviderTemporaryError,
        ),
    ],
)
def test_ocr_process_timeout_and_model_failure_are_classified(
    monkeypatch: pytest.MonkeyPatch,
    raised_error: Exception,
    expected_error: type[Exception],
) -> None:
    def fail_command(*args, **kwargs):
        raise raised_error

    monkeypatch.setattr(subprocess, "run", fail_command)

    with pytest.raises(expected_error):
        _run_command(["ocr"], timeout_seconds=1)
