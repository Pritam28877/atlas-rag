# Atlas Harness: deferred Rust implementation plan

Status: deferred; not approved for implementation. Written on 2026-07-26.

This document preserves a complete Rust path without making Rust part of the
current Python release. The active plan is
[Python-first](02-end-to-end-implementation-plan.md). No Rust toolchain,
dependency, source directory, build step, extension module, sidecar, or runtime
process should be added until the R1 activation decision is explicitly
approved.

The detailed local execution ledger is
`.tasks/atlas-harness-rust-later.json`: 20 ordered parents and 60 subtasks.

## 1. Why defer instead of delete

Rust may later improve process supervision, protocol parsing, high-concurrency
streaming, predictable memory use, and sandbox integration. Those are
hypotheses until the Python harness provides a stable protocol and measured
production profile. Starting now would create two build systems, two runtime
stacks, two persistence clients, and a migration problem before the product
contract is proven.

Deferral produces better evidence:

- Python defines observable behavior and ships user value first.
- Golden schemas, traces, failure fixtures, and security attacks become a
  language-neutral acceptance suite.
- Benchmarks identify which components, if any, need migration.
- Rust can enter behind a versioned process boundary rather than through an
  all-at-once rewrite.
- Every stage retains a tested Python rollback.

## 2. Activation gate

R1 is a hard human gate. Approval must record:

1. the problem Rust is expected to solve;
2. measured Python p50/p95/p99 latency, throughput, peak RSS, task/process
   counts, descriptor counts, cancellation latency, and failure modes;
3. the permitted scope: sidecar, selected kernel services, or full harness;
4. frozen protocol/schema version and checksummed compatibility fixtures;
5. engineering budget, owner, security owner, migration window, and incident
   owner;
6. success thresholds and abort thresholds;
7. the Python release used for rollback; and
8. which components intentionally remain Python.

Language preference, theoretical speed, or successful compilation is not an
activation reason.

## 3. Target boundaries

The eventual Rust system must preserve these separations:

| Boundary | Rust responsibility | Invariant |
| --- | --- | --- |
| Protocol | Validate versioned commands/events and generate language bindings | Payload IDs never prove authority |
| App server | Authenticate local transports, admit bounded work, stream durable sequences | Unknown or privileged commands default deny |
| Journal | Append events and project state transactionally | No acknowledged loss or event rewrite |
| Artifact store | Stream immutable local/S3 blobs and apply retention | No full-buffer large-object path |
| Runtime | Own turn state, tasks, retries, budgets, and cancellation | Resume never resets a budget |
| Provider layer | Compile/decode vendor traffic behind capability contracts | Core does not import vendor semantics |
| Policy/operation | Intersect grants, bind approvals, record side-effect state | Approval never broadens isolation |
| Sandbox | Own child process, OS profile, output, deadline, and cleanup | Missing required control fails closed |
| Extensions | Isolate tools, MCP, plugins, hooks, and parsers | Third-party code never runs in trusted process |
| Context/documents | Build manifests and validate immutable evidence | Memory/model text is never authority |
| Scheduler | Validate bounded DAGs, fence workers, verify handoffs | Child authority is a strict subset |
| Clients/operations | Preserve SDK/client semantics and emit bounded telemetry | No Rust-only protocol fork |

## 4. Ordered delivery phases

Each row is one reviewable parent PR. A parent cannot begin until its listed
predecessor is complete and verified.

| ID | Deliverable | Required exit evidence |
| --- | --- | --- |
| R1 | Activation decision and measurable migration gates | Explicit approval, frozen Python baseline, thresholds, scope, budget, owner, abort and rollback |
| R2 | Source provenance, license, and upstream strategy | Current immutable source audit, file treatment map, NOTICE plan, security-update SLA |
| R3 | Pinned Rust workspace and supply-chain foundation | fmt, clippy with denied warnings, tests, docs, locked build, deny/audit, SBOM, provenance, size and dependency gates |
| R4 | Python-Rust process boundary and ownership model | Authenticated bounded framing, identity, deadline, cancel, health, shutdown, crash and rollback exercises |
| R5 | Canonical protocol and generated compatibility artifacts | Rust/Python/JSON-Schema/TypeScript generation, two-minor reads, rollback unknown retention |
| R6 | Authenticated async app server and transports | Fuzzed framing, server-derived grants, bounded subscribers, durable resume, graceful drain |
| R7 | Event journal and deterministic projections | Concurrent CAS/idempotency, kill durability, Python/Rust replay hash parity |
| R8 | Content-addressed artifacts, retention, and recovery | Streaming local/S3 parity, safe GC, restore hashes, ambiguous-operation classification |
| R9 | Turn runtime and session coordinator | Golden event parity, bounded tool loop, reconnect/cancel/process/task/channel stress |
| R10 | Provider contracts and explainable routing | Vendor-free core, deterministic route snapshots, bounded egress/retry/cost accounting |
| R11 | OpenAI and OpenRouter adapters | Current official-schema evidence, offline conformance, capped live Python/Rust parity |
| R12 | Bedrock, Vertex/Gemini, and local adapters | Identity/region/destination policy, offline matrix, disposable capped live evidence |
| R13 | Policy, approvals, operations, and secrets | Deny/intersection properties, TOCTOU/replay closure, canary-secret containment |
| R14 | Sandbox supervisor and escape closure | Capability detection, owned process trees, escape corpus, privilege reachability gate |
| R15 | Tools and extension ecosystem | Safe built-ins, isolated MCP/plugin/parser host, bounded hooks/skills/instructions |
| R16 | Context, memory, compaction, and document evidence | Exact budgets, critical-fact preservation, bounded memory, verified citations |
| R17 | Durable multi-agent DAG and isolated workers | Graph properties, leases/fencing/CAS recovery, overlay and typed-handoff verification |
| R18 | Clients, observability, evaluation, and replay parity | Cross-client semantics, redaction/cardinality limits, quality/latency/RSS comparison |
| R19 | Dual-run migration, fault drills, and rollback | Effect-free shadow parity, staged ownership, automatic abort, full operational drills |
| R20 | Release hardening and cutover decision | Independent reviews, provider/platform matrix, reproducible package, 24-hour soak, human decision |

## 5. Migration data flow

The migration must remain reversible:

```text
Client
  -> stable Atlas protocol
  -> Python admission/auth/policy owner
  -> versioned authenticated process channel
  -> Rust candidate in shadow mode
  -> canonical decisions/events (no side effects)
  -> parity comparator and bounded divergence journal

After shadow gates:
  read-only ownership
  -> mock-provider ownership
  -> idempotent operation ownership
  -> approved side-effect ownership
  -> optional full control-plane ownership

At every stage:
  health or parity threshold fails
  -> stop new Rust work
  -> drain/cancel owned work
  -> restore Python owner
  -> replay the same durable journal
```

Shadow mode must never duplicate an external side effect. Non-idempotent work
has exactly one active owner. Mixed-version writers are blocked unless the
declared rollback reader can preserve every emitted event and unknown field.

## 6. Compatibility requirements

Rust does not earn ownership until:

- canonical JSON and event hashes match Python golden fixtures;
- current readers accept the prior two minor schemas;
- the declared rollback reader preserves unknown fields and event types;
- writer version gates prevent unsupported rollback data;
- API, CLI, TUI, SDK, and IDE clients reconnect to either implementation;
- provider-normalized text, reasoning metadata, tool calls, usage, errors,
  cancellation, and limits match the canonical contracts;
- journal projections and restored artifacts produce identical hashes; and
- import/export and backup/restore work across the migration boundary.

## 7. Security requirements

Rust reduces some memory-safety risk but does not automatically provide
authorization, sandboxing, cancellation correctness, or secret safety. The
release must still prove:

- server-derived identity and grant binding;
- deny-first policy and approval hash binding;
- destination-bound secrets and egress;
- fail-closed OS isolation;
- environment, mount, network, device, process, and output limits;
- descendant process cleanup;
- extension/parser isolation;
- no secret or PII in logs, telemetry, events, fixtures, or crash data; and
- complete privileged-operation reachability evidence.

Any adapted Codex sandbox or protocol file requires refreshed license,
provenance, local-change, privilege-path, and security-update records.

## 8. Performance and resource proof

For every migrated hot path, publish:

```text
Core path:
Data size variables:
Python baseline:
Rust candidate:
Time complexity:
Space complexity:
Memory growth:
Cancellation behavior:
Hot-path risks:
Threshold result:
What breaks first at scale:
```

The 24-hour soak tracks RSS, allocations where practical, tasks, channels,
queues, connections, file descriptors, subprocesses, subscribers, journal/blob
growth, retry state, provider spend, and shutdown cleanup. A faster median does
not compensate for worse tail latency, correctness, security, or rollback.

## 9. Release and rollback

The release package requires pinned toolchain and dependencies, reproducible
artifacts, vulnerability and license disposition, CycloneDX or SPDX SBOM,
file-level provenance, required notices, cross-platform runner evidence,
provider evidence, runbooks, recovery objectives, and independent security and
license review.

The final human decision may be:

- no cutover: retain Python and archive the evidence;
- partial cutover: keep only measured Rust components behind the process
  boundary; or
- full cutover: move the approved control plane while retaining the Python
  rollback for the declared support window.

No result is considered failure merely because it recommends no migration. The
goal is a better, safer harness—not a predetermined language outcome.
