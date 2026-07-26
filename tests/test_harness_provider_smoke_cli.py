from pathlib import Path

from scripts.run_harness_provider_smoke import main


def test_cli_refuses_live_call_without_explicit_acknowledgement(
    tmp_path: Path,
    capsys,
) -> None:
    database_path = tmp_path / "must-not-exist.sqlite3"
    result_path = tmp_path / "must-not-exist.json"

    exit_code = main(
        (
            "--provider",
            "openai",
            "--model",
            "gpt-smoke",
            "--max-output-tokens",
            "16",
            "--timeout-seconds",
            "10",
            "--cost-cap-microusd",
            "50",
            "--config-path",
            str(tmp_path / "missing.json"),
            "--database-path",
            str(database_path),
            "--result-path",
            str(result_path),
            "--destination-url",
            "https://provider.example/v1",
            "--environment-variable",
            "ATLAS_PROVIDER_SMOKE_KEY",
        )
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == '{"status":"failed"}\n'
    assert not database_path.exists()
    assert not result_path.exists()
