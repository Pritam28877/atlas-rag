import pytest
from pydantic import ValidationError

from app.core.config import MEBIBYTE, Settings


def test_development_settings_have_bounded_policy_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.database.pool_max_size == 10
    assert settings.ingestion.upload_max_bytes == 100 * MEBIBYTE
    assert settings.ingestion.allowed_mime_types == ("application/pdf",)
    assert settings.workers.ocr_concurrency == 1
    assert settings.workers.lifecycle_concurrency == 1
    assert settings.provider_timeouts.request_seconds == 30


def test_nested_environment_values_are_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE__POOL_MAX_SIZE", "20")
    monkeypatch.setenv("INGESTION__PDF_MAX_PAGES", "750")
    monkeypatch.setenv("TELEMETRY__LOG_LEVEL", "WARNING")

    settings = Settings(_env_file=None)

    assert settings.database.pool_max_size == 20
    assert settings.ingestion.pdf_max_pages == 750
    assert settings.telemetry.log_level == "WARNING"


def test_openai_api_key_is_typed_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "private-provider-key")

    settings = Settings(_env_file=None)

    assert settings.openai_api_key is not None
    assert str(settings.openai_api_key) == "**********"
    assert "private-provider-key" not in repr(settings)


def test_database_pool_minimum_cannot_exceed_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE__POOL_MIN_SIZE", "11")
    monkeypatch.setenv("DATABASE__POOL_MAX_SIZE", "10")

    with pytest.raises(ValidationError, match="pool_min_size cannot exceed"):
        Settings(_env_file=None)


def test_policy_rejects_values_above_approved_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INGESTION__UPLOAD_MAX_BYTES", str(500 * MEBIBYTE + 1))

    with pytest.raises(ValidationError, match="less than or equal to"):
        Settings(_env_file=None)


def test_production_requires_infrastructure_settings_without_leaking_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE__URL", "postgresql://user:private@db/rag")

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)

    message = str(error.value)
    assert "missing required production settings" in message
    assert "private" not in message


def test_complete_production_settings_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_values = {
        "APP_ENVIRONMENT": "production",
        "AUTH__ISSUER": "https://identity.example.test/",
        "AUTH__AUDIENCE": "rag-api",
        "AUTH__JWKS_URL": "https://identity.example.test/jwks",
        "DATABASE__URL": "postgresql://user:private@db/rag?sslmode=require",
        "STORAGE__ENDPOINT_URL": "https://storage.internal",
        "STORAGE__BUCKET_NAME": "rag-documents",
        "STORAGE__ACCESS_KEY_ID": "access-key",
        "STORAGE__SECRET_ACCESS_KEY": "secret-key",
        "BROKER__URL": "amqps://user:private@broker/rag",
        "SEARCH__ENDPOINT_URL": "https://search.internal",
        "SEARCH__USERNAME": "search-user",
        "SEARCH__PASSWORD": "search-password",
        "TELEMETRY__METRICS_BEARER_TOKEN": "metrics-token-for-production",
    }
    for name, value in production_values.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert settings.app_environment == "production"
    assert str(settings.database.url) == "**********"


def test_production_scheduler_requires_only_its_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("RUNTIME_ROLE", "scheduler")
    monkeypatch.setenv("BROKER__URL", "amqps://user:private@broker/rag")

    settings = Settings(_env_file=None)

    assert settings.runtime_role == "scheduler"
    assert settings.database.url is None


def test_production_rejects_provider_default_storage_encryption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_values = {
        "APP_ENVIRONMENT": "production",
        "AUTH__ISSUER": "https://identity.example.test/",
        "AUTH__AUDIENCE": "rag-api",
        "AUTH__JWKS_URL": "https://identity.example.test/jwks",
        "DATABASE__URL": "postgresql://user:private@db/rag",
        "STORAGE__ENDPOINT_URL": "https://storage.internal",
        "STORAGE__BUCKET_NAME": "rag-documents",
        "STORAGE__ACCESS_KEY_ID": "access-key",
        "STORAGE__SECRET_ACCESS_KEY": "secret-key",
        "STORAGE__SERVER_SIDE_ENCRYPTION": "provider-default",
        "BROKER__URL": "amqps://user:private@broker/rag",
    }
    for name, value in production_values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match="must explicitly use"):
        Settings(_env_file=None)


@pytest.mark.parametrize("ssl_mode", ["disable", "allow", "prefer"])
def test_production_rejects_database_modes_that_do_not_require_tls(
    monkeypatch: pytest.MonkeyPatch,
    ssl_mode: str,
) -> None:
    production_values = {
        "APP_ENVIRONMENT": "production",
        "AUTH__ISSUER": "https://identity.example.test/",
        "AUTH__AUDIENCE": "rag-api",
        "AUTH__JWKS_URL": "https://identity.example.test/jwks",
        "DATABASE__URL": (
            f"postgresql://user:private@db/rag?sslmode={ssl_mode}"
        ),
        "STORAGE__ENDPOINT_URL": "https://storage.internal",
        "STORAGE__BUCKET_NAME": "rag-documents",
        "STORAGE__ACCESS_KEY_ID": "access-key",
        "STORAGE__SECRET_ACCESS_KEY": "secret-key",
        "BROKER__URL": "amqps://user:private@broker/rag",
        "SEARCH__ENDPOINT_URL": "https://search.internal",
        "SEARCH__USERNAME": "search-user",
        "SEARCH__PASSWORD": "search-password",
        "TELEMETRY__METRICS_BEARER_TOKEN": "metrics-token-for-production",
    }
    for name, value in production_values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match="database must require TLS"):
        Settings(_env_file=None)


def test_production_api_requires_metrics_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")

    with pytest.raises(ValidationError, match="metrics_bearer_token"):
        Settings(_env_file=None)


def test_broker_queue_names_must_be_unique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROKER__NATIVE_QUEUE_NAME", "same-queue")
    monkeypatch.setenv("BROKER__OCR_QUEUE_NAME", "same-queue")

    with pytest.raises(ValidationError, match="queue names must be unique"):
        Settings(_env_file=None)


def test_scheduler_heartbeat_covers_two_reconcile_intervals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIFECYCLE__RECONCILE_INTERVAL_SECONDS", "120")
    monkeypatch.setenv("READINESS__SCHEDULER_HEARTBEAT_MAX_AGE_SECONDS", "180")

    with pytest.raises(ValidationError, match="cover two reconcile intervals"):
        Settings(_env_file=None)
