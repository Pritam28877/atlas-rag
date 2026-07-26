import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "probe_harness_cancellation.py"


def test_real_provider_process_and_durable_cancellation_drill(
    tmp_path: Path,
) -> None:
    os.chmod(tmp_path, 0o700)
    result = subprocess.run(
        (
            sys.executable,
            str(PROBE),
            "--database-path",
            str(tmp_path / "journal.sqlite3"),
            "--pid-path",
            str(tmp_path / "process-ids.json"),
        ),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "evidence_appended": True,
        "failure": {
            "active_resources_at_evidence": 1,
            "event_type": "Turn.Failed",
            "evidence_matches": True,
            "provider_cleaned": True,
            "provider_pending_at_evidence": True,
            "settlement": "containment_failed",
            "state": "needs_operator",
        },
        "journal_verified": True,
        "success": {
            "active_resources": 0,
            "event_type": "Turn.Cancelled",
            "evidence_matches": True,
            "process_returncode": -9,
            "provider_done": True,
            "settlements": {
                "process_tree": "after_escalation",
                "provider_request": "cooperative",
            },
            "state": "cancelled",
        },
    }
