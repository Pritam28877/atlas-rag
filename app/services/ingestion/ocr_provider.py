import csv
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from prometheus_client import Counter, Histogram

from app.core.config import IngestionPolicySettings
from app.services.catalog.enums import VersionState
from app.services.ingestion.errors import TerminalIngestionError
from app.services.ingestion.models import Bounds, NormalizedPage, PageOrigin, TextBlock


class OcrProviderTemporaryError(RuntimeError):
    """Raised when the local OCR runtime fails for a potentially transient reason."""


OCR_PAGE_DURATION_SECONDS = Histogram(
    "rag_ocr_page_duration_seconds",
    "OCR page processing duration by bounded outcome.",
    ["outcome"],
)
OCR_PAGES_TOTAL = Counter(
    "rag_ocr_pages_total",
    "OCR pages processed by bounded outcome.",
    ["outcome"],
)


class OcrProvider(Protocol):
    name: str
    provider_version: str

    def enrich_pages(
        self,
        source_pdf: Path,
        page_numbers: Sequence[int],
        policy: IngestionPolicySettings,
        temporary_directory: Path,
    ) -> tuple[NormalizedPage, ...]: ...


class TesseractOcrProvider:
    name = "tesseract"

    def __init__(self) -> None:
        self._tesseract = _required_command("tesseract")
        self._pdftoppm = _required_command("pdftoppm")
        self.provider_version = _command_version(self._tesseract)

    def enrich_pages(
        self,
        source_pdf: Path,
        page_numbers: Sequence[int],
        policy: IngestionPolicySettings,
        temporary_directory: Path,
    ) -> tuple[NormalizedPage, ...]:
        self._validate_languages(policy.ocr_language_codes)
        pages: list[NormalizedPage] = []
        total_characters = 0
        for page_number in page_numbers:
            started = time.monotonic()
            try:
                rendered_page = self._render_page(
                    source_pdf,
                    page_number,
                    policy,
                    temporary_directory,
                )
                try:
                    page = self._ocr_page(rendered_page, page_number, policy)
                finally:
                    rendered_page.unlink(missing_ok=True)
            except Exception:
                _record_page_metric("failed", started)
                raise
            _record_page_metric("success", started)
            total_characters += len(page.text)
            if total_characters > policy.extracted_text_max_chars:
                raise TerminalIngestionError(
                    VersionState.FAILED,
                    "PROCESSING_OUTPUT_LIMIT_EXCEEDED",
                )
            elapsed_seconds = time.monotonic() - started
            if elapsed_seconds > policy.ocr_page_timeout_seconds:
                raise TerminalIngestionError(
                    VersionState.FAILED,
                    "PROCESSING_RESOURCE_LIMIT_EXCEEDED",
                )
            pages.append(page)
        return tuple(pages)

    def _validate_languages(self, requested_languages: Sequence[str]) -> None:
        completed = _run_command(
            [self._tesseract, "--list-langs"],
            timeout_seconds=30,
            capture_output=True,
        )
        installed_languages = {
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip() and not line.startswith("List of available")
        }
        if not requested_languages or not set(requested_languages).issubset(
            installed_languages
        ):
            raise TerminalIngestionError(
                VersionState.FAILED,
                "OCR_LANGUAGE_UNSUPPORTED",
            )

    def _render_page(
        self,
        source_pdf: Path,
        page_number: int,
        policy: IngestionPolicySettings,
        temporary_directory: Path,
    ) -> Path:
        output_prefix = temporary_directory / f"rendered-{page_number}"
        _run_command(
            [
                self._pdftoppm,
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-singlefile",
                "-r",
                str(policy.ocr_render_dpi),
                "-png",
                str(source_pdf),
                str(output_prefix),
            ],
            timeout_seconds=policy.ocr_page_timeout_seconds,
        )
        rendered_page = output_prefix.with_suffix(".png")
        if not rendered_page.is_file():
            raise OcrProviderTemporaryError("PDF renderer produced no page")
        if rendered_page.stat().st_size > policy.ocr_max_rendered_page_bytes:
            rendered_page.unlink(missing_ok=True)
            raise TerminalIngestionError(
                VersionState.FAILED,
                "PROCESSING_RESOURCE_LIMIT_EXCEEDED",
            )
        return rendered_page

    def _ocr_page(
        self,
        rendered_page: Path,
        page_number: int,
        policy: IngestionPolicySettings,
    ) -> NormalizedPage:
        output_base = rendered_page.with_suffix("")
        language_argument = "+".join(policy.ocr_language_codes)
        _run_command(
            [
                self._tesseract,
                str(rendered_page),
                str(output_base),
                "-l",
                language_argument,
                "--psm",
                "3",
                "tsv",
            ],
            timeout_seconds=policy.ocr_page_timeout_seconds,
        )
        tsv_path = output_base.with_suffix(".tsv")
        try:
            if not tsv_path.is_file():
                raise OcrProviderTemporaryError("OCR provider produced no result")
            if tsv_path.stat().st_size > policy.normalized_max_bytes:
                raise TerminalIngestionError(
                    VersionState.FAILED,
                    "PROCESSING_OUTPUT_LIMIT_EXCEEDED",
                )
            return _parse_tsv(tsv_path, page_number, policy)
        finally:
            tsv_path.unlink(missing_ok=True)


def _parse_tsv(
    path: Path,
    page_number: int,
    policy: IngestionPolicySettings,
) -> NormalizedPage:
    blocks: list[TextBlock] = []
    confidences: list[float] = []
    words: list[str] = []
    with path.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source, delimiter="\t"):
            text = (row.get("text") or "").strip()
            confidence = _parse_confidence(row.get("conf"))
            if not text or confidence < 0:
                continue
            if len(blocks) >= policy.native_max_blocks_per_page:
                raise TerminalIngestionError(
                    VersionState.FAILED,
                    "PROCESSING_RESOURCE_LIMIT_EXCEEDED",
                )
            left = float(row.get("left") or 0)
            top = float(row.get("top") or 0)
            width = float(row.get("width") or 0)
            height = float(row.get("height") or 0)
            blocks.append(
                TextBlock(
                    ordinal=len(blocks),
                    text=text,
                    bounds=Bounds(
                        left=left,
                        bottom=top,
                        right=left + width,
                        top=top + height,
                    ),
                )
            )
            words.append(text)
            confidences.append(confidence)
    extracted_text = " ".join(words)
    if not extracted_text:
        raise TerminalIngestionError(VersionState.FAILED, "OCR_NO_TEXT_DETECTED")
    quality_score = sum(confidences) / (100 * len(confidences))
    if quality_score < policy.ocr_min_confidence:
        raise TerminalIngestionError(
            VersionState.FAILED,
            "OCR_QUALITY_BELOW_THRESHOLD",
        )
    return NormalizedPage(
        page_number=page_number,
        origin=PageOrigin.OCR,
        text=extracted_text,
        blocks=tuple(blocks),
        language_hint="en" if policy.ocr_language_codes == ("eng",) else "und",
        quality_score=round(quality_score, 4),
    )


def _parse_confidence(value: str | None) -> float:
    try:
        return float(value or -1)
    except ValueError:
        return -1


def _required_command(name: str) -> str:
    command = shutil.which(name)
    if command is None:
        raise RuntimeError(f"required OCR command is unavailable: {name}")
    return command


def _command_version(command: str) -> str:
    completed = _run_command([command, "--version"], 30, capture_output=True)
    return completed.stdout.splitlines()[0]


def _run_command(
    arguments: list[str],
    timeout_seconds: int,
    *,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            check=True,
            text=True,
            timeout=timeout_seconds,
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as error:
        raise TerminalIngestionError(
            VersionState.FAILED,
            "PROCESSING_RESOURCE_LIMIT_EXCEEDED",
        ) from error
    except subprocess.CalledProcessError as error:
        raise OcrProviderTemporaryError("OCR command failed") from error


def _record_page_metric(outcome: str, started: float) -> None:
    OCR_PAGES_TOTAL.labels(outcome).inc()
    OCR_PAGE_DURATION_SECONDS.labels(outcome).observe(time.monotonic() - started)
