# Atlas Harness non-live security review evidence

Status: P19 non-live evidence recorded on 2026-07-27.

Control owner: Atlas security owner.

Evidence owner: Atlas harness maintainers.

This record covers the Linux escape corpus and Python privileged-callsite gate
defined by [the threat model](04-threat-model.md). It is reproducible evidence,
not a production-isolation or release approval.

## Verdict and evidence identity

| Review item | Result | Evidence |
| --- | --- | --- |
| P19.1 escape/exfiltration corpus | Pass in the approved degraded-Linux scope | Commit `bd6119f`; 15 checksummed cases |
| P19.2 privileged reachability | Pass with explicit incomplete gates | Commit `1626b34`; 12 classified callsites, 0 registered |
| Production side effects | Blocked | No delegated cgroup, proxy attachment, or seccomp policy |
| Cross-platform containment | Blocked | macOS and Windows backends have no implementation or escape evidence |
| Live-provider security | Not exercised | No OpenAI, OpenRouter, AWS, or GCP request was made |
| Release | Blocked | Open dependency, live-evidence, platform, and license gates remain |

The reviewed corpus is
[`escape_cases.json`](../../tests/harness/security/corpus/escape_cases.json).
Its SHA-256 is
`19d80ba35c053679f10573a7f2594e32008ef6ae89467288f1705698f199366d`.
The callsite evidence is
[`python-privileged-reachability.json`](python-privileged-reachability.json);
the default-deny operation catalog is
[`python-privileged-operation-registry.json`](python-privileged-operation-registry.json).

## Review assumptions

- The review host was Linux `6.17.0-35-generic`, x86-64, with bubblewrap
  `0.9.0`.
- The locked toolchain was uv `0.11.3`, Python `3.12.3`, Node `22.22.0`, and
  npm `10.9.4`.
- The host allowed unprivileged user namespaces and exposed cgroup v2, but did
  not provide a writable delegated cgroup scope.
- Only disposable temporary workspaces, synthetic secrets, loopback fixtures,
  and non-live provider doubles were allowed.
- The GitHub workflow remained disabled by operator direction. The same gates
  are executable locally and are covered by the non-live pytest suite.
- All privileged registry operations remained `planned_disabled`; zero
  operations were promoted to a public registered implementation.

## Reproduction

Run from the repository root with the committed locks unchanged.

### 1. Confirm the host and corpus

```bash
uname -srmo
uv --version
uv run --locked python --version
node --version
npm --version
bwrap --version
sha256sum tests/harness/security/corpus/escape_cases.json
jq '.cases | length' tests/harness/security/corpus/escape_cases.json
```

Expected review-host values are the versions above, the recorded SHA-256, and
`15` corpus cases. A different kernel or bubblewrap build is a new environment,
not equivalent production evidence.

### 2. Run the focused security checks

```bash
uv run --locked pytest -q tests/harness/security
uv run --locked pytest -q tests/harness/sandbox/test_capabilities.py::test_real_linux_probe_reports_explicit_nonproduction_state
uv run --locked python scripts/probe_harness_python_isolation.py
uv run --locked python scripts/verify_harness_privileged_operations.py
uv run --locked python scripts/verify_harness_privileged_reachability.py
```

Observed results:

- `19 passed` in the security folder.
- The real capability test passed and asserted `read_only_degraded`, with side
  effects, syscall filtering, and production resource enforcement disabled.
- The disposable isolation probe returned `status: pass`.
- Workspace read/write, host hiding, environment filtering, explicit
  environment delivery, combined output bounding, descendant cancellation,
  and missing-isolation denial were all `true`.
- The registry reported `19 operations, 0 registered`.
- Reachability reported `12 callsites, 0 registered`.
- The scanner detected API, direct-network, and subprocess bypass patterns; an
  unclassified synthetic subprocess caused manifest validation to fail.

### 3. Reproduce the repository quality gate

```bash
uv run --locked ruff check .
uv run --locked mypy
uv run --locked python scripts/verify_harness_provenance.py
uv run --locked python scripts/verify_harness_privileged_operations.py
uv run --locked python scripts/verify_harness_privileged_reachability.py
uv run --locked python scripts/verify_harness_python_boundaries.py
uv run --locked python scripts/verify_harness_generated_artifacts.py
uv run --locked python scripts/generate_harness_protocol.py --check
npm run typecheck:harness-protocol
uv run --locked python scripts/verify_harness_sbom.py
uv run --locked pytest -q -m "not ocr_integration"
uv run --locked python scripts/verify_harness_packages.py
uv run --locked python scripts/verify_harness_supply_chain.py
```

Observed results were:

- Ruff and mypy passed; mypy checked 268 source files.
- Provenance, privileged-operation, privileged-reachability, package-boundary,
  generated-artifact, generated-protocol, TypeScript, SBOM, and reproducible
  package checks passed.
- The non-live suite passed with `1042 passed, 24 skipped, 4 deselected`.
- The supply-chain policy verified 194 locked Python packages, 16 locked Node
  packages, and 101 installed licenses while retaining two release blockers:
  `py-rust-stemmers==0.1.8` lacks verified license metadata, and the repository
  outbound license still awaits human approval.

## Threat coverage

| Threat | Evidence in this review | Boundary |
| --- | --- | --- |
| T12 host/path escape | Hidden host path and device, declared mount, traversal, and symlink cases | Disposable bubblewrap environment only |
| T13 command ambiguity | Structured executable/argument profile; no shell dispatch in the supervisor | Registered tool execution remains disabled |
| T14 process/resource escape | New session, owned tree termination, wall/output/process profile bounds | CPU, memory, and PID cgroup enforcement not claimed |
| T15 network exfiltration | Isolated network and denied direct connection; provider sink inventory | Proxy-mediated allowlisting not implemented |
| T16 missing isolation | Missing backend denies; current host is `read_only_degraded` | Side effects remain denied |
| T17 privileged bypass | Exact AST inventory plus synthetic API/network/process bypass tests | Known direct Python APIs only |
| T18 inherited secret | Parent secret absent; per-call FIFO delivery; child output canary redacted | Production secret broker path remains unregistered |
| T19 secret output | Exact synthetic canaries are filtered from combined output | General semantic PII detection is not claimed |
| T36 dependency risk | Locked sources, audits, SBOM, reproducible wheel and sdist | Two license release blockers remain |

## Privileged-path gaps

Every operation-backed callsite accounts for the complete gate set: a gate has
either an existing source-symbol reference or an explicit missing entry.
Incomplete paths cannot be registered by the validator.

| Callsite | Missing gates | Required disposition |
| --- | --- | --- |
| Vertex credential discovery | cancellation | Keep opt-in and unregistered until refresh is cancellable or killable |
| Vertex credential refresh | cancellation | Keep opt-in and unregistered until refresh is cancellable or killable |
| Bubblewrap version probe | audit, authorization, idempotency, policy, sandbox | Internal capability detection only |
| Bubblewrap containment probe | audit, authorization, idempotency, policy | Internal capability detection only |
| Sandbox supervisor spawn | audit, authorization, idempotency, policy | Internal primitive only; add a fully gated execution facade before registration |

## Residual risks and closure owners

| Risk | Owner | Closure evidence |
| --- | --- | --- |
| No cgroup delegation, seccomp policy, or proxy attachment | Atlas sandbox owner | Production-mode capability report plus repeated escape suite |
| Vertex refresh cannot be cancelled once its blocking thread starts | Atlas provider owner | Bounded killable worker or cancellable transport with cancellation test |
| Static inventory recognizes a reviewed catalog of direct Python sinks; reflection, native extensions, or a new SDK API require catalog updates | Atlas security owner | Independent code review and a synthetic test for each newly admitted sink family |
| Source-symbol references prove checked-in evidence exists, not a formal whole-program call graph | Atlas security owner | Registration review must trace the complete entrypoint-to-sink path |
| macOS and Windows containment are unavailable | Platform owners | Native backend, capability detection, and platform-specific escape corpus |
| Live provider behavior and cloud policy were not tested | Atlas provider owner | Approved P14.3/P15.3 live evidence with redacted artifacts |
| P19 depends on incomplete P18 production evidence | Atlas harness owner | P18 dependency closes before P19 parent status changes to done |
| Two license exceptions block release | Dependency and provenance owners | Verified upstream license metadata and approved outbound repository license |

An independent reviewer should stop if the corpus hash changes, a callsite is
unclassified or stale, any verifier fails, the registry count becomes nonzero
without a registration review, or the host differs without a new evidence
record. Secrets are not required for this reproduction.
