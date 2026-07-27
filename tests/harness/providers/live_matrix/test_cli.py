from pathlib import Path
from types import SimpleNamespace

import scripts.build_harness_live_matrix as live_matrix_cli


def test_cli_reports_release_state_without_evidence_content(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    captured: list[Path] = []

    async def build(path: Path):
        captured.append(path)
        return SimpleNamespace(release_ready=False)

    monkeypatch.setattr(
        live_matrix_cli,
        "build_live_matrix_from_manifest",
        build,
    )
    manifest_path = tmp_path / "manifest.json"

    exit_code = live_matrix_cli.main(
        ("--manifest-path", str(manifest_path))
    )

    output = capsys.readouterr()
    assert exit_code == 0
    assert captured == [manifest_path]
    assert output.out == (
        '{"release_ready":false,"status":"completed"}\n'
    )
    assert output.err == ""


def test_cli_failure_is_generic(tmp_path: Path, capsys) -> None:
    exit_code = live_matrix_cli.main(
        ("--manifest-path", str(tmp_path / "missing.json"))
    )

    output = capsys.readouterr()
    assert exit_code == 1
    assert output.out == ""
    assert output.err == '{"status":"failed"}\n'
