# Supply-chain blocker status

Checked: 2026-07-27. The automated verifier currently reports two
release-blocking exceptions; this record does not waive either one.

## `py-rust-stemmers==0.1.8`

The installed wheel exposes no `License-Expression`, legacy `License`, or
license classifier, so the repository verifier correctly keeps this exception
release-blocking. The upstream PyPI JSON description says the project is MIT
licensed and is the only current external evidence:

<https://pypi.org/pypi/py-rust-stemmers/0.1.8/json>

Dependency/provenance owners must verify that claim against the exact source and
wheel contents, record the evidence and attribution, and update the policy
through review. A prose claim alone is not enough for the automated gate.

## Repository outbound license

`rag-api==0.1.0` remains `pending_human_approval` in
`docs/harness/provenance-manifest.json`. No root `LICENSE` has been added and no
human approval is inferred from the code or package build. The outbound license
exception remains release-blocking until an approver records scope,
compatibility, date, and distribution constraints.

## Current command

```bash
uv run --locked python scripts/verify_harness_supply_chain.py
```

Expected current result: 194 locked Python packages, 16 locked Node packages,
101 installed licenses, and 2 release blockers.
