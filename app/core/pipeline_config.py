from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator

from app.core.config_base import MEBIBYTE, ImmutableSettingsModel


class IngestionPolicySettings(ImmutableSettingsModel):
    allowed_mime_types: tuple[str, ...] = ("application/pdf",)
    upload_max_bytes: int = Field(default=100 * MEBIBYTE, ge=1, le=500 * MEBIBYTE)
    pdf_max_pages: int = Field(default=500, ge=1, le=2500)
    pdf_allow_embedded_files: bool = False
    pdf_max_content_stream_bytes_per_page: int = Field(
        default=16 * MEBIBYTE,
        ge=1,
        le=32 * MEBIBYTE,
    )
    normalized_max_bytes: int = Field(
        default=256 * MEBIBYTE,
        ge=1,
        le=512 * MEBIBYTE,
    )
    extracted_text_max_chars: int = Field(default=20_000_000, ge=1, le=50_000_000)
    native_min_text_chars_per_page: int = Field(default=20, ge=1, le=1000)
    native_max_blocks_per_page: int = Field(default=10_000, ge=1, le=50_000)
    max_chunks: int = Field(default=10_000, ge=1, le=25_000)
    chunk_max_tokens: int = Field(default=96, ge=16, le=126)
    chunk_overlap_tokens: int = Field(default=16, ge=0, le=64)
    chunk_page_max_chars: int = Field(default=2_000_000, ge=1_000, le=5_000_000)
    queue_age_alert_seconds: int = Field(default=900, ge=1, le=3600)
    ocr_language_codes: tuple[str, ...] = ("eng",)
    ocr_min_confidence: float = Field(default=0.70, ge=0, le=0.90)
    ocr_render_dpi: int = Field(default=300, ge=150, le=400)
    ocr_page_timeout_seconds: int = Field(default=120, ge=1, le=300)
    ocr_max_rendered_page_bytes: int = Field(
        default=64 * MEBIBYTE,
        ge=MEBIBYTE,
        le=128 * MEBIBYTE,
    )

    @model_validator(mode="after")
    def validate_supported_policy(self) -> Self:
        if self.allowed_mime_types != ("application/pdf",):
            raise ValueError("version 1 supports only application/pdf")
        if self.pdf_allow_embedded_files:
            raise ValueError("embedded PDF files are not supported")
        if self.chunk_overlap_tokens >= self.chunk_max_tokens:
            raise ValueError("chunk overlap must be smaller than chunk size")
        return self


class EmbeddingSettings(ImmutableSettingsModel):
    provider: Literal["fastembed"] = "fastembed"
    model_name: str = (
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    model_revision: str = Field(
        default="faf4aa4225822f3bc6376869cb1164e8e3feedd0",
        pattern=r"^[0-9a-f]{40}$",
    )
    model_version: str = Field(
        default="faf4aa4225822f3bc6376869cb1164e8e3feedd0-fastembed-0.8.0-mean",
        min_length=1,
        max_length=128,
    )
    model_directory: str = "/opt/rag/models/multilingual-minilm"
    model_sha256: str = Field(
        default=(
            "634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99"
        ),
        pattern=r"^[0-9a-f]{64}$",
    )
    dimensions: int = Field(default=384, ge=1, le=4096)
    batch_size: int = Field(default=16, ge=1, le=64)
    max_concurrent_batches: int = Field(default=1, ge=1, le=4)


class SearchSettings(ImmutableSettingsModel):
    endpoint_url: str | None = None
    username: str | None = None
    password: SecretStr | None = None
    index_name: str = Field(
        default="rag-chunks-v1", pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$"
    )
    target_version: str = "opensearch-2.19.5-hybrid-v1"
    verify_tls: bool = True
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=120)

    @model_validator(mode="after")
    def validate_credentials(self) -> Self:
        if bool(self.username) != bool(self.password):
            raise ValueError("search username and password must be paired")
        if self.endpoint_url and not self.endpoint_url.startswith(
            ("http://", "https://")
        ):
            raise ValueError("search endpoint must use HTTP or HTTPS")
        return self


class ProviderTimeoutSettings(ImmutableSettingsModel):
    connect_seconds: float = Field(default=5.0, gt=0, le=30)
    request_seconds: float = Field(default=30.0, gt=0, le=300)
    embedding_seconds: float = Field(default=60.0, gt=0, le=300)
    storage_seconds: float = Field(default=30.0, gt=0, le=120)
