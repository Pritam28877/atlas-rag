"""Recursive redaction contract tests."""

from app.services.harness.observability import redact


def test_nested_canaries_are_redacted_and_bounded() -> None:
    result = redact(
        {
            "prompt": "private prompt",
            "nested": {"Authorization": "Bearer secret", "safe": "ok"},
            "items": list(range(100)),
        }
    )
    assert result.value["prompt"] == "<redacted>"
    assert result.value["nested"]["Authorization"] == "<redacted>"
    assert result.truncated
    assert "private prompt" not in str(result.value)
