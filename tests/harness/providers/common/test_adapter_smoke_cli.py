from pathlib import Path

import scripts.run_harness_adapter_smoke as adapter_smoke_cli


def test_cli_dispatches_one_vertex_smoke(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    captured = []

    async def run(authorized):
        captured.append(authorized)

    monkeypatch.setattr(
        adapter_smoke_cli,
        "run_vertex_adapter_smoke",
        run,
    )
    exit_code = adapter_smoke_cli.main(
        _arguments(
            tmp_path,
            provider="vertex",
            disposable_project_id="atlas-smoke-12345",
            cost_cap_microusd="10000",
        )
    )

    assert exit_code == 0
    assert len(captured) == 1
    assert captured[0].provider == "vertex"
    assert capsys.readouterr().out == '{"status":"completed"}\n'


def test_cli_dispatches_local_with_capability_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured = []

    async def run(authorized):
        captured.append(authorized)

    monkeypatch.setattr(
        adapter_smoke_cli,
        "run_local_adapter_smoke",
        run,
    )
    exit_code = adapter_smoke_cli.main(
        _arguments(
            tmp_path,
            provider="local-compatible",
            cost_cap_microusd="0",
            capability_evidence_path=(
                tmp_path / "capabilities.json"
            ),
        )
    )

    assert exit_code == 0
    assert len(captured) == 1
    assert captured[0].capability_evidence_path is not None


def test_cli_failure_is_generic_and_runs_no_provider(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls = []

    async def run(authorized):
        calls.append(authorized)

    monkeypatch.setattr(
        adapter_smoke_cli,
        "run_local_adapter_smoke",
        run,
    )
    arguments = _arguments(
        tmp_path,
        provider="local-compatible",
        cost_cap_microusd="0",
        capability_evidence_path=tmp_path / "capabilities.json",
    )
    arguments.remove("--acknowledge-live-costs")

    exit_code = adapter_smoke_cli.main(arguments)

    assert exit_code == 1
    assert calls == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"status":"failed"}\n'


def _arguments(
    tmp_path: Path,
    *,
    provider: str,
    cost_cap_microusd: str,
    disposable_project_id: str | None = None,
    capability_evidence_path: Path | None = None,
) -> list[str]:
    arguments = [
        "--acknowledge-live-costs",
        "--provider",
        provider,
        "--model",
        "configured-smoke-model",
        "--max-output-tokens",
        "32",
        "--timeout-seconds",
        "10",
        "--cost-cap-microusd",
        cost_cap_microusd,
        "--gate-environment-variable",
        "ATLAS_ADAPTER_SMOKE_ENABLED",
        "--configuration-path",
        str(tmp_path / "providers.json"),
        "--route-policy-path",
        str(tmp_path / "route.json"),
        "--identity-path",
        str(tmp_path / "identity.json"),
        "--database-path",
        str(tmp_path / "smoke.sqlite3"),
        "--result-path",
        str(tmp_path / "result.json"),
    ]
    if disposable_project_id is not None:
        arguments.extend(
            ["--disposable-project-id", disposable_project_id]
        )
    if capability_evidence_path is not None:
        arguments.extend(
            [
                "--capability-evidence-path",
                str(capability_evidence_path),
            ]
        )
    return arguments
