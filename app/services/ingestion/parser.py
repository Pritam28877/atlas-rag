import math
from collections.abc import Callable, Mapping
from importlib.metadata import version
from pathlib import Path
from typing import Protocol, cast

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.core.config import IngestionPolicySettings
from app.services.catalog.enums import VersionState
from app.services.ingestion.errors import TerminalIngestionError
from app.services.ingestion.models import (
    Bounds,
    DocumentRoute,
    InspectionReport,
    NormalizedPage,
    PageOrigin,
    TextBlock,
)


class NativePdfParser(Protocol):
    name: str
    parser_version: str

    def parse(
        self,
        source: Path,
        policy: IngestionPolicySettings,
        page_sink: Callable[[NormalizedPage], None],
    ) -> InspectionReport: ...


class PypdfNativeParser:
    """Page-at-a-time native extraction behind a provider-neutral contract."""

    name = "pypdf"
    parser_version = version("pypdf")

    def parse(
        self,
        source: Path,
        policy: IngestionPolicySettings,
        page_sink: Callable[[NormalizedPage], None],
    ) -> InspectionReport:
        try:
            reader = PdfReader(source, strict=True)
            if reader.is_encrypted:
                raise TerminalIngestionError(
                    VersionState.REJECTED,
                    "PDF_ENCRYPTED_UNSUPPORTED",
                )
            page_count = len(reader.pages)
            if page_count < 1:
                raise TerminalIngestionError(
                    VersionState.FAILED,
                    "PDF_MALFORMED",
                )
            if page_count > policy.pdf_max_pages:
                raise TerminalIngestionError(
                    VersionState.REJECTED,
                    "PDF_PAGE_LIMIT_EXCEEDED",
                )
            if _has_active_or_embedded_content(reader):
                raise TerminalIngestionError(
                    VersionState.QUARANTINED,
                    "ACTIVE_CONTENT_DETECTED",
                )
            return self._extract_pages(reader, policy, page_sink)
        except TerminalIngestionError:
            raise
        except (PdfReadError, KeyError, TypeError, ValueError) as error:
            raise TerminalIngestionError(
                VersionState.FAILED,
                "PDF_MALFORMED",
            ) from error

    def _extract_pages(
        self,
        reader: PdfReader,
        policy: IngestionPolicySettings,
        page_sink: Callable[[NormalizedPage], None],
    ) -> InspectionReport:
        native_page_count = 0
        extracted_text_chars = 0
        ocr_page_numbers: list[int] = []
        document_warnings: set[str] = set()
        for page_index, page in enumerate(reader.pages):
            page_number = page_index + 1
            contents = page.get_contents()
            if (
                contents is not None
                and len(contents.get_data())
                > policy.pdf_max_content_stream_bytes_per_page
            ):
                raise TerminalIngestionError(
                    VersionState.FAILED,
                    "PROCESSING_RESOURCE_LIMIT_EXCEEDED",
                )
            blocks: list[TextBlock] = []

            def collect_block(
                text: str,
                _current_matrix: list[float],
                text_matrix: list[float],
                _font_dictionary: Mapping[str, object] | None,
                font_size: float,
            ) -> None:
                normalized_text = text.strip()
                if not normalized_text:
                    return
                if len(blocks) >= policy.native_max_blocks_per_page:
                    raise TerminalIngestionError(
                        VersionState.FAILED,
                        "PROCESSING_RESOURCE_LIMIT_EXCEEDED",
                    )
                bounds = _text_bounds(text_matrix, font_size, normalized_text)
                blocks.append(
                    TextBlock(
                        ordinal=len(blocks),
                        text=normalized_text,
                        bounds=bounds,
                    )
                )

            extracted_text = (
                page.extract_text(visitor_text=collect_block) or ""
            ).strip()
            extracted_text_chars += len(extracted_text)
            if extracted_text_chars > policy.extracted_text_max_chars:
                raise TerminalIngestionError(
                    VersionState.FAILED,
                    "PROCESSING_OUTPUT_LIMIT_EXCEEDED",
                )
            warnings: list[str] = []
            if len(extracted_text) < policy.native_min_text_chars_per_page:
                origin = PageOrigin.OCR
                ocr_page_numbers.append(page_number)
                warnings.append("NATIVE_TEXT_BELOW_THRESHOLD")
                document_warnings.add("OCR_REQUIRED")
            else:
                origin = PageOrigin.NATIVE
                native_page_count += 1
            page_sink(
                NormalizedPage(
                    page_number=page_number,
                    origin=origin,
                    text=extracted_text,
                    blocks=tuple(blocks),
                    language_hint=_language_hint(extracted_text),
                    quality_score=_quality_score(extracted_text),
                    warnings=tuple(warnings),
                )
            )
        page_count = len(reader.pages)
        route = _route(native_page_count, page_count)
        return InspectionReport(
            parser_name=self.name,
            parser_version=self.parser_version,
            page_count=page_count,
            route=route,
            native_page_count=native_page_count,
            ocr_page_numbers=tuple(ocr_page_numbers),
            extracted_text_chars=extracted_text_chars,
            warnings=tuple(sorted(document_warnings)),
        )


def _resolve_mapping(value: object) -> Mapping[str, object]:
    if hasattr(value, "get_object"):
        resolver = cast(_ResolvableObject, value)
        resolved = resolver.get_object()
    else:
        resolved = value
    if isinstance(resolved, Mapping):
        return cast(Mapping[str, object], resolved)
    return {}


class _ResolvableObject(Protocol):
    def get_object(self) -> object: ...


def _has_active_or_embedded_content(reader: PdfReader) -> bool:
    root = _resolve_mapping(reader.trailer["/Root"])
    names = _resolve_mapping(root.get("/Names"))
    if names.get("/EmbeddedFiles") is not None:
        return True
    actions: list[object] = [root.get("/OpenAction"), root.get("/AA")]
    for page in reader.pages:
        page_mapping = _resolve_mapping(page)
        actions.extend([page_mapping.get("/AA"), page_mapping.get("/OpenAction")])
    for unresolved_action in actions:
        action = _resolve_mapping(unresolved_action)
        if str(action.get("/S")) in {
            "/JavaScript",
            "/URI",
            "/Launch",
            "/SubmitForm",
        }:
            return True
    return False


def _text_bounds(
    text_matrix: list[float], font_size: float, text: str
) -> Bounds | None:
    if len(text_matrix) < 6 or not math.isfinite(font_size):
        return None
    left = float(text_matrix[4])
    bottom = float(text_matrix[5])
    if not math.isfinite(left) or not math.isfinite(bottom):
        return None
    height = max(float(font_size), 0.0)
    approximate_width = max(len(text), 1) * height * 0.5
    return Bounds(
        left=left,
        bottom=bottom,
        right=left + approximate_width,
        top=bottom + height,
    )


def _quality_score(text: str) -> float:
    if not text:
        return 0.0
    printable_characters = sum(character.isprintable() for character in text)
    return round(printable_characters / len(text), 4)


def _language_hint(text: str) -> str:
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return "und"
    ascii_letters = sum(character.isascii() for character in letters)
    return "en" if ascii_letters / len(letters) >= 0.8 else "und"


def _route(native_page_count: int, page_count: int) -> DocumentRoute:
    if native_page_count == 0:
        return DocumentRoute.OCR
    if native_page_count < page_count:
        return DocumentRoute.MIXED
    return DocumentRoute.NATIVE
