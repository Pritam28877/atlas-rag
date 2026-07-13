from functools import lru_cache
from typing import Literal, Self

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MEBIBYTE = 1024 * 1024


class ImmutableSettingsModel(BaseModel):
    """Base model for configuration that cannot change after startup."""

    model_config = {"frozen": True}


class DatabaseSettings(ImmutableSettingsModel):
    url: SecretStr | None = None
    pool_min_size: int = Field(default=2, ge=1, le=20)
    pool_max_size: int = Field(default=10, ge=1, le=100)
    pool_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @model_validator(mode="after")
    def validate_pool_bounds(self) -> Self:
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("pool_min_size cannot exceed pool_max_size")
        return self


class StorageSettings(ImmutableSettingsModel):
    endpoint_url: str | None = None
    region: str = "us-east-1"
    bucket_name: str | None = None
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None
    use_tls: bool = True
    server_side_encryption: Literal["AES256", "aws:kms"] = "AES256"
    signed_url_ttl_seconds: int = Field(default=900, ge=60, le=3600)


class BrokerSettings(ImmutableSettingsModel):
    url: SecretStr | None = None
    use_tls: bool = True
    visibility_timeout_seconds: int = Field(default=3600, ge=60, le=21600)
    prefetch_multiplier: int = Field(default=1, ge=1, le=16)
    message_max_bytes: int = Field(default=65_536, ge=1024, le=262_144)


class WorkerSettings(ImmutableSettingsModel):
    native_concurrency: int = Field(default=2, ge=1, le=4)
    ocr_concurrency: int = Field(default=1, ge=1, le=2)
    native_parse_timeout_seconds: int = Field(default=300, ge=1, le=900)
    ocr_timeout_seconds: int = Field(default=900, ge=1, le=1800)
    native_parse_memory_mib: int = Field(default=2048, ge=128, le=4096)
    ocr_memory_mib: int = Field(default=512, ge=128, le=1024)
    job_max_attempts: int = Field(default=3, ge=1, le=5)
    retry_base_seconds: int = Field(default=30, ge=1, le=60)
    retry_max_seconds: int = Field(default=900, ge=1, le=3600)

    @model_validator(mode="after")
    def validate_retry_bounds(self) -> Self:
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ValueError("retry_base_seconds cannot exceed retry_max_seconds")
        return self


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
    max_chunks: int = Field(default=10_000, ge=1, le=25_000)
    queue_age_alert_seconds: int = Field(default=900, ge=1, le=3600)
    ocr_language_codes: tuple[str, ...] = ("eng",)
    ocr_min_confidence: float = Field(default=0.70, ge=0, le=0.90)

    @model_validator(mode="after")
    def validate_supported_policy(self) -> Self:
        if self.allowed_mime_types != ("application/pdf",):
            raise ValueError("version 1 supports only application/pdf")
        if self.pdf_allow_embedded_files:
            raise ValueError("embedded PDF files are not supported")
        return self


class TelemetrySettings(ImmutableSettingsModel):
    service_name: str = Field(default="rag-api", min_length=1, max_length=100)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    otlp_endpoint: str | None = None
    traces_enabled: bool = False
    metrics_enabled: bool = True


class ProviderTimeoutSettings(ImmutableSettingsModel):
    connect_seconds: float = Field(default=5.0, gt=0, le=30)
    request_seconds: float = Field(default=30.0, gt=0, le=300)
    embedding_seconds: float = Field(default=60.0, gt=0, le=300)
    storage_seconds: float = Field(default=30.0, gt=0, le=120)


class Settings(BaseSettings):
    """Validated application settings loaded from environment variables."""

    app_name: str = Field(default="RAG API", min_length=1, max_length=100)
    app_environment: Literal["development", "test", "production"] = "development"
    api_v1_prefix: str = Field(default="/v1", pattern=r"^/[a-zA-Z0-9/_-]*$")
    openai_api_key: SecretStr | None = None
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    broker: BrokerSettings = Field(default_factory=BrokerSettings)
    workers: WorkerSettings = Field(default_factory=WorkerSettings)
    ingestion: IngestionPolicySettings = Field(default_factory=IngestionPolicySettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    provider_timeouts: ProviderTimeoutSettings = Field(
        default_factory=ProviderTimeoutSettings
    )

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    @model_validator(mode="after")
    def require_production_infrastructure(self) -> Self:
        if self.app_environment != "production":
            return self

        required_values = {
            "database.url": self.database.url,
            "storage.endpoint_url": self.storage.endpoint_url,
            "storage.bucket_name": self.storage.bucket_name,
            "storage.access_key_id": self.storage.access_key_id,
            "storage.secret_access_key": self.storage.secret_access_key,
            "broker.url": self.broker.url,
        }
        missing_fields = [name for name, value in required_values.items() if not value]
        if missing_fields:
            missing_names = ", ".join(missing_fields)
            raise ValueError(f"missing required production settings: {missing_names}")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one immutable settings instance per process."""
    return Settings()
