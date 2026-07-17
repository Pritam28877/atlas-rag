from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.services.catalog.enums import VersionState


class PageOrigin(StrEnum):
    NATIVE = "native"
    OCR = "ocr"
    MIXED = "mixed"


class DocumentRoute(StrEnum):
    NATIVE = "native"
    OCR = "ocr"
    MIXED = "mixed"


class Bounds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    left: float
    bottom: float
    right: float
    top: float


class TextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=0)
    text: str
    bounds: Bounds | None = None


class NormalizedPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page_number: int = Field(ge=1)
    origin: PageOrigin
    text: str
    blocks: tuple[TextBlock, ...]
    language_hint: str
    quality_score: float = Field(ge=0, le=1)
    warnings: tuple[str, ...] = ()


class InspectionReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "inspection-v1"
    parser_name: str
    parser_version: str
    page_count: int = Field(ge=1)
    route: DocumentRoute
    native_page_count: int = Field(ge=0)
    ocr_page_numbers: tuple[int, ...]
    extracted_text_chars: int = Field(ge=0)
    warnings: tuple[str, ...] = ()


class TerminalInspectionReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "inspection-v1"
    parser_name: str
    parser_version: str
    outcome_state: VersionState
    reason_code: str
    page_count: int | None = Field(default=None, ge=1)
