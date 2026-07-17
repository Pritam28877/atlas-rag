import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import TextIO
from uuid import UUID

from app.services.ingestion.models import (
    DocumentRoute,
    InspectionReport,
    NormalizedPage,
    PageOrigin,
    TerminalInspectionReport,
    TextBlock,
)


class PageArtifactWriter:
    """Incrementally write bounded JSONL pages without retaining the document."""

    def __init__(
        self,
        path: Path,
        document_version_id: UUID,
        parser_name: str,
        parser_version: str,
    ) -> None:
        self._path = path
        self._document_version_id = document_version_id
        self._output = path.open("x", encoding="utf-8")
        self._page_count = 0
        self._write_record(
            {
                "record_type": "header",
                "schema_version": "normalized-pages-v1",
                "document_version_id": str(document_version_id),
                "parser_name": parser_name,
                "parser_version": parser_version,
            }
        )

    def write_page(self, page: NormalizedPage) -> None:
        self._write_record(
            {
                "record_type": "page",
                "document_version_id": str(self._document_version_id),
                "page": page.model_dump(mode="json"),
            }
        )
        self._page_count += 1

    def finish(self, inspection: InspectionReport) -> None:
        if self._page_count != inspection.page_count:
            raise ValueError("page artifact count does not match inspection")
        self._write_record(
            {
                "record_type": "manifest",
                "page_count": self._page_count,
                "route": inspection.route,
                "ocr_page_numbers": inspection.ocr_page_numbers,
            }
        )
        self._output.flush()
        self._output.close()

    def close(self) -> None:
        if not self._output.closed:
            self._output.close()

    def _write_record(self, record: dict[str, object]) -> None:
        json.dump(record, self._output, ensure_ascii=False, separators=(",", ":"))
        self._output.write("\n")


def write_inspection(path: Path, inspection: InspectionReport) -> None:
    path.write_text(
        inspection.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )


def write_terminal_inspection(
    path: Path,
    inspection: TerminalInspectionReport,
) -> None:
    path.write_text(
        inspection.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def iter_page_artifact(
    path: Path,
    expected_version_id: UUID,
) -> Iterator[NormalizedPage]:
    expected_page_number = 1
    manifest_page_count: int | None = None
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            record = json.loads(line)
            if record.get("document_version_id") not in {
                None,
                str(expected_version_id),
            }:
                raise ValueError("page artifact document version does not match")
            record_type = record.get("record_type")
            if line_number == 1 and record_type != "header":
                raise ValueError("page artifact header is missing")
            if record_type == "page":
                page = NormalizedPage.model_validate(record.get("page"))
                if page.page_number != expected_page_number:
                    raise ValueError("page artifact ordering is invalid")
                expected_page_number += 1
                yield page
            elif record_type == "manifest":
                manifest_page_count = int(record.get("page_count", -1))
    if manifest_page_count != expected_page_number - 1:
        raise ValueError("page artifact manifest count does not match records")


def write_ocr_pages(
    path: Path,
    document_version_id: UUID,
    provider_name: str,
    provider_version: str,
    source_page_count: int,
    pages: Sequence[NormalizedPage],
) -> None:
    with path.open("x", encoding="utf-8") as output:
        _write_json_line(
            output,
            {
                "record_type": "header",
                "schema_version": "ocr-pages-v1",
                "document_version_id": str(document_version_id),
                "provider_name": provider_name,
                "provider_version": provider_version,
                "source_page_count": source_page_count,
            },
        )
        for page in pages:
            _write_json_line(
                output,
                {
                    "record_type": "page",
                    "document_version_id": str(document_version_id),
                    "page": page.model_dump(mode="json"),
                },
            )
        _write_json_line(
            output,
            {
                "record_type": "manifest",
                "document_version_id": str(document_version_id),
                "source_page_count": source_page_count,
                "selected_page_count": len(pages),
                "page_numbers": [page.page_number for page in pages],
            },
        )


def merge_ocr_pages(
    source_path: Path,
    destination_path: Path,
    document_version_id: UUID,
    ocr_pages: Sequence[NormalizedPage],
    generator_version: str,
) -> InspectionReport:
    ocr_by_page = {page.page_number: page for page in ocr_pages}
    if len(ocr_by_page) != len(ocr_pages):
        raise ValueError("OCR page output contains duplicate page numbers")
    writer = PageArtifactWriter(
        destination_path,
        document_version_id,
        "native+ocr",
        generator_version,
    )
    native_page_count = 0
    total_characters = 0
    page_count = 0
    try:
        for native_page in iter_page_artifact(source_path, document_version_id):
            page_count += 1
            ocr_page = ocr_by_page.pop(native_page.page_number, None)
            merged_page = (
                _merge_page(native_page, ocr_page)
                if ocr_page is not None
                else native_page
            )
            if merged_page.origin is PageOrigin.NATIVE:
                native_page_count += 1
            total_characters += len(merged_page.text)
            writer.write_page(merged_page)
        if ocr_by_page:
            raise ValueError("OCR output references a page outside the source artifact")
        route = (
            DocumentRoute.OCR
            if native_page_count == 0
            else DocumentRoute.MIXED
        )
        inspection = InspectionReport(
            parser_name="native+ocr",
            parser_version=generator_version,
            page_count=page_count,
            route=route,
            native_page_count=native_page_count,
            ocr_page_numbers=tuple(page.page_number for page in ocr_pages),
            extracted_text_chars=total_characters,
            warnings=("OCR_ENRICHED",),
        )
        writer.finish(inspection)
        return inspection
    finally:
        writer.close()


def _merge_page(
    native_page: NormalizedPage,
    ocr_page: NormalizedPage,
) -> NormalizedPage:
    if not native_page.text.strip():
        return ocr_page
    combined_blocks = list(native_page.blocks)
    for block in ocr_page.blocks:
        combined_blocks.append(
            TextBlock(
                ordinal=len(combined_blocks),
                text=block.text,
                bounds=block.bounds,
            )
        )
    warnings = tuple(sorted(set(native_page.warnings) | {"OCR_ENRICHED"}))
    return NormalizedPage(
        page_number=native_page.page_number,
        origin=PageOrigin.MIXED,
        text=f"{native_page.text}\n{ocr_page.text}",
        blocks=tuple(combined_blocks),
        language_hint=ocr_page.language_hint,
        quality_score=min(native_page.quality_score, ocr_page.quality_score),
        warnings=warnings,
    )


def _write_json_line(output: TextIO, record: Mapping[str, object]) -> None:
    json.dump(record, output, ensure_ascii=False, separators=(",", ":"))
    output.write("\n")
