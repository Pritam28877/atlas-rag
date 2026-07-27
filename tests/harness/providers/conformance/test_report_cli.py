from pathlib import Path

import scripts.run_harness_conformance as report_cli


def test_cli_runs_packaged_suite_without_provider_content(
    tmp_path: Path,
    capsys,
) -> None:
    tmp_path.chmod(0o700)
    result_path = tmp_path / "conformance.json"

    exit_code = report_cli.main(
        (
            "--timeout-seconds",
            "30",
            "--result-path",
            str(result_path),
        )
    )

    output = capsys.readouterr()
    assert exit_code == 0
    assert output.out == (
        '{"adapters":6,"scenarios":8,"status":"completed"}\n'
    )
    assert output.err == ""
    assert result_path.exists()


def test_cli_refuses_existing_output_with_generic_failure(
    tmp_path: Path,
    capsys,
) -> None:
    tmp_path.chmod(0o700)
    result_path = tmp_path / "conformance.json"
    result_path.write_text("existing")
    result_path.chmod(0o600)

    exit_code = report_cli.main(
        (
            "--timeout-seconds",
            "30",
            "--result-path",
            str(result_path),
        )
    )

    output = capsys.readouterr()
    assert exit_code == 1
    assert output.out == ""
    assert output.err == '{"status":"failed"}\n'
    assert result_path.read_text() == "existing"
