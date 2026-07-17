import json
from pathlib import Path
from uuid import UUID

import pytest

from app.core.config import IngestionPolicySettings
from app.services.catalog.enums import VersionState
from app.services.ingestion.artifacts import PageArtifactWriter
from app.services.ingestion.errors import TerminalIngestionError
from app.services.ingestion.models import DocumentRoute, PageOrigin
from app.services.ingestion.parser import PypdfNativeParser

FIXTURES = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures"
VERSION_ID = UUID("1dd21a4f-c0dc-45b4-8e36-5b9c9a7012a9")


def parse_fixture(name: str, policy: IngestionPolicySettings | None = None):
    parser = PypdfNativeParser()
    pages = []
    report = parser.parse(
        FIXTURES / name,
        policy or IngestionPolicySettings(),
        pages.append,
    )
    return report, pages


def test_native_pdf_preserves_page_text_blocks_and_quality_signals() -> None:
    report, pages = parse_fixture("native-simple-en-001.pdf")

    assert report.route is DocumentRoute.NATIVE
    assert report.page_count == 1
    assert report.ocr_page_numbers == ()
    assert pages[0].page_number == 1
    assert pages[0].origin is PageOrigin.NATIVE
    assert "stable first-page citation" in pages[0].text
    assert pages[0].blocks
    assert pages[0].language_hint == "en"
    assert pages[0].quality_score > 0.9


@pytest.mark.parametrize(
    ("fixture_name", "route", "ocr_pages"),
    [
        ("scan-en-001.pdf", DocumentRoute.OCR, (1,)),
        ("mixed-en-001.pdf", DocumentRoute.MIXED, (2,)),
    ],
)
def test_low_text_pages_are_routed_to_ocr_with_page_evidence(
    fixture_name: str,
    route: DocumentRoute,
    ocr_pages: tuple[int, ...],
) -> None:
    report, pages = parse_fixture(fixture_name)

    assert report.route is route
    assert report.ocr_page_numbers == ocr_pages
    for page_number in ocr_pages:
        assert pages[page_number - 1].origin is PageOrigin.OCR
        assert "NATIVE_TEXT_BELOW_THRESHOLD" in pages[page_number - 1].warnings


@pytest.mark.parametrize(
    ("fixture_name", "state", "reason_code"),
    [
        (
            "encrypted-001.pdf",
            VersionState.REJECTED,
            "PDF_ENCRYPTED_UNSUPPORTED",
        ),
        ("corrupt-001.pdf", VersionState.FAILED, "PDF_MALFORMED"),
        (
            "suspicious-active-content-001.pdf",
            VersionState.QUARANTINED,
            "ACTIVE_CONTENT_DETECTED",
        ),
    ],
)
def test_preflight_has_deterministic_safe_terminal_outcomes(
    fixture_name: str,
    state: VersionState,
    reason_code: str,
) -> None:
    with pytest.raises(TerminalIngestionError) as captured:
        parse_fixture(fixture_name)

    assert captured.value.state is state
    assert captured.value.reason_code == reason_code


def test_page_and_text_output_limits_fail_before_publication() -> None:
    with pytest.raises(
        TerminalIngestionError,
        match="PDF_PAGE_LIMIT_EXCEEDED",
    ):
        parse_fixture(
            "limit-breach-001.pdf",
            IngestionPolicySettings(pdf_max_pages=1),
        )

    with pytest.raises(
        TerminalIngestionError,
        match="PROCESSING_OUTPUT_LIMIT_EXCEEDED",
    ):
        parse_fixture(
            "native-simple-en-001.pdf",
            IngestionPolicySettings(extracted_text_max_chars=10),
        )


def test_page_artifact_is_incremental_and_manifested(tmp_path: Path) -> None:
    report, pages = parse_fixture("mixed-en-001.pdf")
    artifact_path = tmp_path / "pages.jsonl"
    writer = PageArtifactWriter(
        artifact_path,
        VERSION_ID,
        "pypdf",
        "6.14.2",
    )
    for page in pages:
        writer.write_page(page)
    writer.finish(report)

    artifact_lines = artifact_path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in artifact_lines]
    assert records[0]["record_type"] == "header"
    assert records[0]["document_version_id"] == str(VERSION_ID)
    page_records = records[1:-1]
    assert all(
        record["document_version_id"] == str(VERSION_ID)
        for record in page_records
    )
    assert [record["page"]["page_number"] for record in records[1:-1]] == [1, 2]
    assert records[-1] == {
        "record_type": "manifest",
        "page_count": 2,
        "route": "mixed",
        "ocr_page_numbers": [2],
    }
