# Atlas Harness requirement evidence audit

This audit is the release evidence index. A green unit or type check is not a
substitute for an environment, security, license, or operator result. The
current candidate is intentionally **blocked** until every open row has direct
evidence.

## Evidence map

| Requirement area | Authoritative implementation/evidence | Current state | Release consequence |
| --- | --- | --- | --- |
| Protocol, journal, projection, and retention | `app/services/harness/protocol/`, `app/services/harness/journal/`, `tests/test_harness_*` | Non-live tests pass | Keep open if a durability drill is missing |
| Provider adapters and live matrix | `docs/harness/08-vertex-local-live-smokes.md`, `docs/harness/09-cross-provider-live-matrix.md`, `scripts/run_harness_*smoke.py` | P14.3/P15.3 still in progress | Blocks release |
| Policy, operations, sandbox, and security | `docs/harness/04-threat-model.md`, `docs/harness/10-security-review-evidence.md`, `docs/harness/python-privileged-reachability.json` | Linux host is read-only degraded; independent review is pending | Blocks production claim |
| CLI, TUI, SDK, IDE reconnect | `app/cli/harness/`, `sdk/typescript/`, `tests/harness/cli/`, `tests/harness/sdk/` | Contract coverage exists; live reconnect evidence is pending | Blocks release if not recorded |
| Replay, fixtures, and benchmarks | `app/services/harness/evaluation/`, `tests/harness/evaluation/` | Non-live reports and regression gates pass | Does not close live gates |
| Backup, restore, rollback, and drills | `app/services/harness/recovery/`, `docs/harness/12-operations-runbook.md` | Disposable contracts and procedures are recorded | Execute and retain drill evidence |
| Provenance, licenses, SBOM, and packages | `docs/harness/05-provenance-and-license.md`, `scripts/verify_harness_{provenance,supply_chain,sbom,packages}.py` | Known release blockers remain in supply-chain policy | Blocks release |
| Soak and human approval | `docs/harness/14-independent-review-and-soak.md`, `docs/harness/15-release-decision.md` | Not executed or signed | Blocks release |

## Reproduction gate

From the repository root, capture output in a private evidence directory:

```bash
evidence_dir="$(mktemp -d)"
umask 077
uv run --locked pytest -q -m 'not ocr_integration' \
  | tee "$evidence_dir/non-live-pytest.log"
uv run --locked python scripts/verify_harness_provenance.py \
  | tee "$evidence_dir/provenance.log"
uv run --locked python scripts/verify_harness_generated_artifacts.py \
  | tee "$evidence_dir/generated.log"
uv run --locked python scripts/verify_harness_python_boundaries.py \
  | tee "$evidence_dir/boundaries.log"
```

The evidence record must include the exact revision, UTC start/end, command
exit status, skipped-test reasons, environment identity, and the raw logs. A
row remains open when any of those fields or its environment-specific result is
missing.
