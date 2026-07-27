from pathlib import Path
from types import SimpleNamespace

import scripts.prepare_harness_live_matrix as preparation_cli


def test_cli_reports_provider_count_without_manifest_content(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    captured: list[tuple[Path, Path, Path]] = []

    async def prepare(
        report_path: Path,
        preparation_path: Path,
        manifest_path: Path,
    ):
        captured.append(
            (report_path, preparation_path, manifest_path)
        )
        return SimpleNamespace(providers=tuple(range(6)))

    monkeypatch.setattr(
        preparation_cli,
        "prepare_live_matrix_manifest",
        prepare,
    )
    paths = tuple(tmp_path / name for name in ("report", "input", "output"))

    exit_code = preparation_cli.main(
        (
            "--conformance-report-path",
            str(paths[0]),
            "--preparation-path",
            str(paths[1]),
            "--manifest-path",
            str(paths[2]),
        )
    )

    output = capsys.readouterr()
    assert exit_code == 0
    assert captured == [paths]
    assert output.out == '{"providers":6,"status":"completed"}\n'
    assert output.err == ""


def test_cli_failure_is_generic(tmp_path: Path, capsys) -> None:
    exit_code = preparation_cli.main(
        (
            "--conformance-report-path",
            str(tmp_path / "missing-report"),
            "--preparation-path",
            str(tmp_path / "missing-input"),
            "--manifest-path",
            str(tmp_path / "manifest"),
        )
    )

    output = capsys.readouterr()
    assert exit_code == 1
    assert output.out == ""
    assert output.err == '{"status":"failed"}\n'
