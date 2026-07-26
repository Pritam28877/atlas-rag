from pathlib import Path

import scripts.run_harness_bedrock_smoke as bedrock_smoke_cli
from app.cli.harness import AdmittedBedrockSmokeLaunch


def test_cli_refuses_live_calls_without_explicit_acknowledgement(
    tmp_path: Path,
    capsys,
) -> None:
    database_path = tmp_path / "must-not-exist.sqlite3"
    result_path = tmp_path / "must-not-exist.json"

    exit_code = bedrock_smoke_cli.main(
        (
            "--grant-path",
            str(tmp_path / "missing-grant.json"),
            "--configuration-path",
            str(tmp_path / "missing-providers.json"),
            "--route-policy-path",
            str(tmp_path / "missing-policy.json"),
            "--identity-path",
            str(tmp_path / "missing-identity.json"),
            "--database-path",
            str(database_path),
            "--result-path",
            str(result_path),
            "--signing-key-environment-variable",
            "ATLAS_BEDROCK_GRANT_KEY",
        )
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == '{"status":"failed"}\n'
    assert not database_path.exists()
    assert not result_path.exists()


def test_cli_wires_acknowledged_private_paths(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    captured: list[AdmittedBedrockSmokeLaunch] = []

    async def run(launch: AdmittedBedrockSmokeLaunch) -> None:
        captured.append(launch)

    monkeypatch.setattr(bedrock_smoke_cli, "run_bedrock_smoke", run)
    exit_code = bedrock_smoke_cli.main(
        (
            "--acknowledge-live-costs",
            "--grant-path",
            str(tmp_path / "grant.json"),
            "--configuration-path",
            str(tmp_path / "providers.json"),
            "--route-policy-path",
            str(tmp_path / "policy.json"),
            "--identity-path",
            str(tmp_path / "identity.json"),
            "--database-path",
            str(tmp_path / "evidence.sqlite3"),
            "--result-path",
            str(tmp_path / "result.json"),
            "--signing-key-environment-variable",
            "ATLAS_BEDROCK_GRANT_KEY",
        )
    )

    output = capsys.readouterr()
    assert exit_code == 0
    assert output.out == '{"status":"completed"}\n'
    assert output.err == ""
    assert len(captured) == 1
    assert captured[0].acknowledged
