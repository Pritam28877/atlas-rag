from pathlib import Path

import scripts.run_harness_local_probe as local_probe_cli


def test_cli_dispatches_six_call_probe(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    captured = []

    async def run(authorized):
        captured.append(authorized)

    monkeypatch.setattr(
        local_probe_cli,
        "run_local_capability_probe",
        run,
    )

    exit_code = local_probe_cli.main(_arguments(tmp_path))

    assert exit_code == 0
    assert len(captured) == 1
    assert captured[0].maximum_provider_calls == 6
    assert capsys.readouterr().out == '{"status":"completed"}\n'


def test_cli_refuses_missing_acknowledgement(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls = []

    async def run(authorized):
        calls.append(authorized)

    monkeypatch.setattr(
        local_probe_cli,
        "run_local_capability_probe",
        run,
    )
    arguments = _arguments(tmp_path)
    arguments.remove("--acknowledge-live-probe")

    exit_code = local_probe_cli.main(arguments)

    assert exit_code == 1
    assert calls == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"status":"failed"}\n'


def _arguments(tmp_path: Path) -> list[str]:
    return [
        "--acknowledge-live-probe",
        "--model",
        "configured-local-model",
        "--per-case-timeout-seconds",
        "10",
        "--evidence-ttl-seconds",
        "900",
        "--gate-environment-variable",
        "ATLAS_LOCAL_PROBE_ENABLED",
        "--configuration-path",
        str(tmp_path / "providers.json"),
        "--route-policy-path",
        str(tmp_path / "route.json"),
        "--identity-path",
        str(tmp_path / "identity.json"),
        "--result-path",
        str(tmp_path / "capabilities.json"),
    ]
