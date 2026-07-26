import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.harness_session_resume_fixtures import TURN_ID
from scripts.probe_harness_session_resume import CRASH_EXIT_CODE

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "probe_harness_session_resume.py"


def run_probe(
    mode: str,
    database_path: Path,
    invocation_path: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            str(PROBE),
            "--database-path",
            str(database_path),
            "--invocation-path",
            str(invocation_path),
            "--mode",
            mode,
        ),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_crash_reconnect_duplicate_produces_one_logical_turn(
    tmp_path: Path,
) -> None:
    os.chmod(tmp_path, 0o700)
    database_path = tmp_path / "journal.sqlite3"
    invocation_path = tmp_path / "inner-invocations.txt"

    crashed = run_probe("crash", database_path, invocation_path)
    assert crashed.returncode == CRASH_EXIT_CODE
    assert crashed.stdout == ""

    reconnected = run_probe("reconnect", database_path, invocation_path)
    assert reconnected.returncode == 0, reconnected.stderr
    report = json.loads(reconnected.stdout)

    assert report == {
        "acknowledged_sequence": 3,
        "append_status": "idempotent_replay",
        "delivered_sequence": 5,
        "duplicate_result_matches": True,
        "event_count": 1,
        "inner_invocations": 2,
        "journal_verified": True,
        "receipt_result_matches": True,
        "result_text": TURN_ID,
    }
