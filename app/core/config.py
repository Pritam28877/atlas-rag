from functools import lru_cache
from typing import Literal, Self
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config_base import MEBIBYTE, ImmutableSettingsModel
from app.core.harness_config import HarnessSettings
from app.core.pipeline_config import (
    EmbeddingSettings,
    IngestionPolicySettings,
    ProviderTimeoutSettings,
    SearchSettings,
)


class DatabaseSettings(ImmutableSettingsModel):
    url: SecretStr | None = None
    pool_min_size: int = Field(default=2, ge=1, le=20)
    pool_max_size: int = Field(default=10, ge=1, le=100)
    pool_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)

    @model_validator(mode="after")
    def validate_pool_bounds(self) -> Self:
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("pool_min_size cannot exceed pool_max_size")
        return self


class AuthSettings(ImmutableSettingsModel):
    issuer: str | None = None
    audience: str | None = None
    jwks_url: str | None = None
    tenant_claim: str = Field(default="tenant_id", min_length=1, max_length=100)
    jwks_cache_ttl_seconds: int = Field(default=300, ge=60, le=3600)

    @model_validator(mode="after")
    def validate_oidc_pair(self) -> Self:
        configured = (self.issuer, self.audience, self.jwks_url)
        if any(configured) and not all(configured):
            raise ValueError(
                "auth issuer, audience, and jwks_url must be configured together"
            )
        if self.jwks_url is not None and not self.jwks_url.startswith("https://"):
            raise ValueError("auth jwks_url must use HTTPS")
        if self.issuer is not None and not self.issuer.startswith("https://"):
            raise ValueError("auth issuer must use HTTPS")
        return self


class StorageSettings(ImmutableSettingsModel):
    endpoint_url: str | None = None
    region: str = "us-east-1"
    bucket_name: str | None = None
    key_prefix: str = Field(default="rag", pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None
    use_tls: bool = True
    server_side_encryption: Literal["provider-default", "AES256", "aws:kms"] = (
        "AES256"
    )
    kms_key_id: str | None = None
    signed_url_ttl_seconds: int = Field(default=900, ge=60, le=3600)

    @model_validator(mode="after")
    def validate_encryption_settings(self) -> Self:
        if self.server_side_encryption == "aws:kms" and not self.kms_key_id:
            raise ValueError("kms_key_id is required for aws:kms encryption")
        return self


class BrokerSettings(ImmutableSettingsModel):
    url: SecretStr | None = None
    use_tls: bool = True
    tls_ca_cert_path: str | None = None
    tls_cert_path: str | None = None
    tls_key_path: str | None = None
    prefetch_multiplier: int = Field(default=1, ge=1, le=16)
    message_max_bytes: int = Field(default=65_536, ge=1024, le=262_144)
    queue_max_messages: int = Field(default=10_000, ge=1, le=1_000_000)
    queue_max_bytes: int = Field(
        default=256 * MEBIBYTE,
        ge=MEBIBYTE,
        le=4096 * MEBIBYTE,
    )
    queue_message_ttl_seconds: int = Field(default=86_400, ge=900, le=604_800)
    heartbeat_seconds: int = Field(default=30, ge=10, le=120)
    connection_max_retries: int = Field(default=5, ge=1, le=20)
    connection_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    native_queue_name: str = Field(default="ingestion.native", min_length=1)
    ocr_queue_name: str = Field(default="ingestion.ocr", min_length=1)
    publication_queue_name: str = Field(
        default="ingestion.publication", min_length=1
    )
    lifecycle_queue_name: str = Field(default="ingestion.lifecycle", min_length=1)
    dead_letter_exchange_name: str = Field(
        default="ingestion.dead-letter.exchange",
        min_length=1,
    )
    dead_letter_queue_name: str = Field(
        default="ingestion.dead-letter",
        min_length=1,
    )

    @model_validator(mode="after")
    def validate_transport_security(self) -> Self:
        if self.url is not None:
            broker_url = self.url.get_secret_value()
            if self.use_tls != broker_url.startswith("amqps://"):
                raise ValueError("broker URL scheme must match use_tls")
        if bool(self.tls_cert_path) != bool(self.tls_key_path):
            raise ValueError("broker TLS client certificate and key must be paired")
        queue_names = {
            self.native_queue_name,
            self.ocr_queue_name,
            self.publication_queue_name,
            self.lifecycle_queue_name,
            self.dead_letter_queue_name,
        }
        if len(queue_names) != 5:
            raise ValueError("broker queue names must be unique")
        return self


class WorkerSettings(ImmutableSettingsModel):
    native_concurrency: int = Field(default=2, ge=1, le=4)
    ocr_concurrency: int = Field(default=1, ge=1, le=2)
    publication_concurrency: int = Field(default=1, ge=1, le=4)
    lifecycle_concurrency: int = Field(default=1, ge=1, le=4)
    native_parse_timeout_seconds: int = Field(default=300, ge=1, le=900)
    ocr_timeout_seconds: int = Field(default=900, ge=1, le=1800)
    publication_timeout_seconds: int = Field(default=900, ge=1, le=1800)
    lifecycle_timeout_seconds: int = Field(default=300, ge=1, le=900)
    native_parse_memory_mib: int = Field(default=2048, ge=128, le=4096)
    ocr_memory_mib: int = Field(default=512, ge=128, le=1024)
    publication_memory_mib: int = Field(default=1024, ge=512, le=2048)
    lifecycle_memory_mib: int = Field(default=512, ge=128, le=1024)
    native_cpu_limit: float = Field(default=2.0, ge=0.25, le=8.0)
    ocr_cpu_limit: float = Field(default=1.0, ge=0.25, le=8.0)
    publication_cpu_limit: float = Field(default=2.0, ge=0.25, le=8.0)
    lifecycle_cpu_limit: float = Field(default=1.0, ge=0.25, le=8.0)
    job_temp_directory: str = "/var/lib/rag-jobs"
    shutdown_grace_seconds: int = Field(default=30, ge=1, le=300)
    job_max_attempts: int = Field(default=3, ge=1, le=5)
    max_tasks_per_child: int = Field(default=25, ge=1, le=1000)
    retry_base_seconds: int = Field(default=30, ge=1, le=60)
    retry_max_seconds: int = Field(default=900, ge=1, le=3600)

    @model_validator(mode="after")
    def validate_retry_bounds(self) -> Self:
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ValueError("retry_base_seconds cannot exceed retry_max_seconds")
        return self


class TelemetrySettings(ImmutableSettingsModel):
    service_name: str = Field(default="rag-api", min_length=1, max_length=100)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    otlp_endpoint: str | None = None
    traces_enabled: bool = False
    metrics_enabled: bool = True
    metrics_bearer_token: SecretStr | None = Field(
        default=None,
        min_length=16,
        max_length=4096,
    )
    metrics_cache_ttl_seconds: float = Field(default=15.0, ge=1, le=60)
    metrics_query_timeout_seconds: float = Field(default=2.0, ge=0.1, le=10)


class ReadinessSettings(ImmutableSettingsModel):
    cache_ttl_seconds: float = Field(default=5.0, ge=1, le=30)
    scheduler_heartbeat_max_age_seconds: int = Field(
        default=180,
        ge=30,
        le=10_800,
    )


class LifecycleSettings(ImmutableSettingsModel):
    delete_batch_size: int = Field(default=250, ge=1, le=1000)
    reconcile_page_size: int = Field(default=200, ge=1, le=1000)
    stale_job_seconds: int = Field(default=900, ge=60, le=86400)
    reconcile_interval_seconds: int = Field(default=60, ge=10, le=3600)


class Settings(BaseSettings):
    """Validated application settings loaded from environment variables."""

    app_name: str = Field(default="RAG API", min_length=1, max_length=100)
    app_environment: Literal["development", "test", "production"] = "development"
    runtime_role: Literal[
        "api",
        "native-worker",
        "ocr-worker",
        "publication-worker",
        "lifecycle-worker",
        "scheduler",
    ] = "api"
    api_v1_prefix: str = Field(default="/v1", pattern=r"^/[a-zA-Z0-9/_-]*$")
    api_request_max_bytes: int = Field(default=65_536, ge=1024, le=MEBIBYTE)
    openai_api_key: SecretStr | None = None
    auth: AuthSettings = Field(default_factory=AuthSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    broker: BrokerSettings = Field(default_factory=BrokerSettings)
    workers: WorkerSettings = Field(default_factory=WorkerSettings)
    ingestion: IngestionPolicySettings = Field(default_factory=IngestionPolicySettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    lifecycle: LifecycleSettings = Field(default_factory=LifecycleSettings)
    readiness: ReadinessSettings = Field(default_factory=ReadinessSettings)
    harness: HarnessSettings = Field(default_factory=HarnessSettings)
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
        minimum_heartbeat_age = self.lifecycle.reconcile_interval_seconds * 2
        if (
            self.readiness.scheduler_heartbeat_max_age_seconds
            < minimum_heartbeat_age
        ):
            raise ValueError(
                "scheduler heartbeat maximum age must cover two reconcile intervals"
            )
        if self.app_environment != "production":
            return self

        storage_roles = {
            "api",
            "native-worker",
            "ocr-worker",
            "publication-worker",
            "lifecycle-worker",
        }
        search_roles = {"api", "publication-worker", "lifecycle-worker"}
        database_roles = storage_roles
        if (
            self.runtime_role in storage_roles
            and self.storage.server_side_encryption == "provider-default"
        ):
            raise ValueError(
                "production storage must explicitly use AES256 or aws:kms encryption"
            )
        required_values: dict[str, object | None] = {"broker.url": self.broker.url}
        if self.runtime_role in database_roles:
            required_values["database.url"] = self.database.url
        if self.runtime_role == "api":
            required_values.update(
                {
                    "auth.issuer": self.auth.issuer,
                    "auth.audience": self.auth.audience,
                    "auth.jwks_url": self.auth.jwks_url,
                }
            )
            if self.telemetry.metrics_enabled:
                required_values["telemetry.metrics_bearer_token"] = (
                    self.telemetry.metrics_bearer_token
                )
        if self.runtime_role in storage_roles:
            required_values.update(
                {
                    "storage.endpoint_url": self.storage.endpoint_url,
                    "storage.bucket_name": self.storage.bucket_name,
                    "storage.access_key_id": self.storage.access_key_id,
                    "storage.secret_access_key": self.storage.secret_access_key,
                }
            )
        if self.runtime_role in search_roles:
            required_values.update(
                {
                    "search.endpoint_url": self.search.endpoint_url,
                    "search.username": self.search.username,
                    "search.password": self.search.password,
                }
            )
        missing_fields = [name for name, value in required_values.items() if not value]
        if missing_fields:
            missing_names = ", ".join(missing_fields)
            raise ValueError(f"missing required production settings: {missing_names}")
        if self.runtime_role in search_roles and (
            not self.search.verify_tls
            or not str(self.search.endpoint_url).startswith("https://")
        ):
            raise ValueError("production search must use verified HTTPS")
        if self.runtime_role in storage_roles and (
            not self.storage.use_tls
            or not str(self.storage.endpoint_url).startswith("https://")
        ):
            raise ValueError("production storage must use HTTPS")
        if self.broker.url is None:
            raise ValueError("production broker settings are incomplete")
        if not self.broker.use_tls or not self.broker.url.get_secret_value().startswith(
            "amqps://"
        ):
            raise ValueError("production broker must use AMQPS")
        if self.runtime_role in database_roles:
            if self.database.url is None:
                raise ValueError("production database settings are incomplete")
            database_url = self.database.url.get_secret_value()
            ssl_modes = parse_qs(urlsplit(database_url).query).get("sslmode", [])
            if len(ssl_modes) != 1 or ssl_modes[0] not in {
                "require",
                "verify-ca",
                "verify-full",
            }:
                raise ValueError("production database must require TLS")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one immutable settings instance per process."""
    return Settings()
