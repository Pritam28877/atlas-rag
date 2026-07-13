from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_local_platform_harness_keeps_services_loopback_only() -> None:
    compose = (PROJECT_ROOT / "docker" / "compose.local-platform.yml").read_text(
        encoding="utf-8"
    )

    assert "127.0.0.1:${P2_MINIO_API_PORT:-19000}:9000" in compose
    assert "127.0.0.1:${P2_RABBITMQ_AMQP_PORT:-15672}:5672" in compose
    assert "p2-minio-data" in compose
    assert "p2-rabbitmq-data" in compose
