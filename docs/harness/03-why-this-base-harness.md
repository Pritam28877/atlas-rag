# Why Python is the best current base for Atlas Harness

Status: Python-first architecture decision, revised on 2026-07-26.

## Decision

Use this repository's Python 3.12, uv, FastAPI, Pydantic, SQLAlchemy,
PostgreSQL, Celery, S3-compatible storage, and OpenTelemetry foundations as the
current Atlas Harness base. Implement one Atlas-owned protocol and state
machine. Keep untrusted tools, MCP servers, plugins, hooks, and parsers outside
the FastAPI process behind OS isolation and resource controls.

Codex, OpenCode, Jcode, Claude Code, Orqen, and documentIntelligence remain
pinned design evidence. Current production code is independently implemented
in Python unless a later provenance record explicitly permits file-level reuse.
Rust is deferred, not rejected: it has a separate activation-gated migration
plan after Python contracts and bottlenecks are measured.

See the [Mermaid architecture](01-best-of-breed-architecture.md) and the
[implementation plan](02-end-to-end-implementation-plan.md).

## 1. What “best” means

A base is eligible only if it passes every hard gate:

1. **Legal reuse:** an identifiable license permits modification and
   redistribution.
2. **Security boundary:** the design includes enforceable OS isolation and an
   explicit deny state, not approval UX alone.
3. **Durable protocol:** sessions and side effects have typed, resumable,
   machine-consumable state.
4. **Extensibility:** providers, tools, MCP, skills, plugins, and clients can be
   added without rewriting the turn loop.
5. **Bounded operation:** concurrency, queues, retries, output, context, cost,
   and retention can be capped.
6. **Verification path:** the source exposes seams for deterministic tests,
   replay, telemetry, and failure injection.
7. **Maintainability:** the implementation fits the current repository,
   operations, dependency workflow, and engineering ownership.

After hard gates, compare provider portability, context quality, multi-agent
coordination, client experience, performance, and operational maturity. A
marketing benchmark, star count, or feature count cannot override a failed hard
gate.

## 2. Gate comparison

| Candidate | Reusable source/license | OS execution boundary | Durable typed control protocol | Direct-base result |
| --- | --- | --- | --- | --- |
| Current Python repository | Existing project source; outbound license still needs approval | Must add a fail-closed out-of-process supervisor; Python itself is not a sandbox | FastAPI/Pydantic/SQLAlchemy provide typed boundaries and durable integration seams | **Selected current base; lowest integration risk, hardening required** |
| Codex | Yes, Apache-2.0 | Linux sandbox and executable policy are source-visible, but privileged app-server bypasses must be removed | App server models Thread, Turn, Item, streaming events, resume/fork/compact | Behavior/security evidence now; possible future Rust input only |
| OpenCode | Yes, MIT | Its own security policy says it is not a sandbox; host tools and in-process plugins remain trusted | Strong server/SDK and new event journal, but legacy and new stacks coexist | Conditional; adapt modules, not whole base |
| Jcode | Yes, MIT | Normal shell/tool paths do not provide a general OS sandbox; some hook failures are fail-open | Strong daemon/session design; stable API bridge is incomplete | Conditional; adapt daemon, DAG, and compaction patterns |
| Claude Code | Public behavior docs; repository license is all-rights-reserved | Documented Seatbelt/bubblewrap sandbox and permissions | Documented sessions, hooks, agents, teams, worktrees, checkpoints | Fail legal-source gate; independent behavior specification only |
| Local Orqen | Source audited; no visible license | No coding-tool or OS-sandbox kernel | Strong versioned graphs, idempotent leased runs, deterministic scheduler, recovery, providers, replay | Fail legal/security base gates; adapt concepts only after permission |
| Local documentIntelligence | Source audited; no visible license | No coding sandbox; host parsers need isolation | Typed runtime/events, bounded tool-loop patterns, artifacts, grounded corpus workflow; permissions/resume incomplete | Fail direct-base gates; adapt document/runtime contracts after permission |

The public repositories are pinned; the two local working trees were reviewed
read-only on 2026-07-25 and are not treated as redistributable source.

## 3. Why Python wins the current delivery decision

### It integrates with the system that already exists

The repository already has typed settings, authentication, FastAPI lifecycle
ownership, SQLAlchemy repositories, Alembic migrations, PostgreSQL integration
tests, S3-compatible artifact storage, bounded Celery workers, OpenTelemetry,
and uv-locked dependencies. A Python harness can reuse those operational
boundaries without a second daemon, build system, configuration stack, storage
client, deployment image, or incident model.

This is a delivery advantage, not a claim that Python is inherently safer.
Security-critical code remains small and explicit, and untrusted execution is
never allowed inside the API process.

### Isolation comes from the OS boundary, not the language

Python cannot sandbox arbitrary Python, shell, plugin, MCP, or parser code in
process. Atlas therefore treats each as an untrusted owned process. Policy,
approval, destination, environment, mounts, resource limits, output filtering,
audit, deadline, cancellation, and process-tree cleanup are applied before and
after spawn. Required controls fail closed when unavailable.

Codex's sandbox and privileged-RPC findings remain useful negative and positive
security evidence. Atlas independently implements those requirements rather
than importing the Rust app server now.

### Pydantic is the protocol source of truth

Strict Pydantic v2 models define Principal, Grant, Workspace, Thread, Turn,
Item, Event, Operation, Approval, Task, Artifact, Context, Provider, and
Evaluation records. They deterministically generate JSON Schema and strict
TypeScript declarations. FastAPI is an adapter around that domain; it does not
own the turn state machine or authorize from payload identifiers.

The protocol lets API, CLI, TUI, IDE, SDK, and future Rust components share one
semantic model. Version gates, unknown-field retention, bounded cursors,
idempotency, and durable sequence resume are language-independent.

### Async ownership is explicit and testable

Every `asyncio` task, queue, semaphore, subprocess, provider request, database
transaction, stream, and subscriber has an owner, bound, deadline,
cancellation path, and shutdown behavior. CPU-heavy or blocking SDK work runs
in bounded workers rather than the event loop. Crash and cancellation tests
verify the resource model instead of assuming it from language choice.

### The future Rust path remains clean

The current interfaces deliberately separate domain, adapters, and process
supervision. The [deferred Rust plan](07-rust-deferred-implementation-plan.md)
starts only after a Python protocol freeze and benchmark baseline. It requires
cross-language golden fixtures, shadow execution without duplicate effects,
staged ownership, and a tested Python rollback. This avoids paying migration
cost before evidence shows where Rust materially improves the product.

## 4. What each other harness contributes

### OpenCode: provider and event architecture

Best parts to adapt:

- provider-independent canonical messages, tools, stream events, errors,
  request compilation, transport, and redaction;
- an atomic SQLite event append plus projection model with monotonic aggregate
  sequences and replay divergence checks;
- per-session run serialization with cross-session concurrency;
- schema-first HTTP APIs and generated SDKs;
- rich MCP transports and OAuth lifecycle;
- progressive tool discovery through a bounded AST interpreter; and
- anchored context summaries and old-output pruning.

Do not copy unchanged:

- the transitional dual architecture;
- unrestricted host execution;
- optional server authentication outside loopback;
- arbitrary in-process npm plugins;
- control loops whose retry, repeated-tool, step, or concurrency ceilings remain
  incomplete or permissive; or
- approximate `characters / 4` token accounting.

Sources:
[provider-independent LLM package](https://github.com/anomalyco/opencode/tree/7534d23551f665e65080809975b4ca5c7d63807b/packages/llm),
[event journal](https://github.com/anomalyco/opencode/blob/7534d23551f665e65080809975b4ca5c7d63807b/packages/core/src/event.ts),
[run coordinator](https://github.com/anomalyco/opencode/blob/7534d23551f665e65080809975b4ca5c7d63807b/packages/core/src/session/run-coordinator.ts),
[MCP implementation](https://github.com/anomalyco/opencode/blob/7534d23551f665e65080809975b4ca5c7d63807b/packages/opencode/src/mcp/index.ts),
[Code Mode implementation](https://github.com/anomalyco/opencode/tree/7534d23551f665e65080809975b4ca5c7d63807b/packages/codemode),
[new runner status](https://github.com/anomalyco/opencode/blob/7534d23551f665e65080809975b4ca5c7d63807b/packages/core/src/session/runner/llm.ts),
[legacy retry implementation](https://github.com/anomalyco/opencode/blob/7534d23551f665e65080809975b4ca5c7d63807b/packages/opencode/src/session/retry.ts),
[token estimator](https://github.com/anomalyco/opencode/blob/7534d23551f665e65080809975b4ca5c7d63807b/packages/core/src/util/token.ts),
and [security policy](https://github.com/anomalyco/opencode/blob/7534d23551f665e65080809975b4ca5c7d63807b/SECURITY.md).

### Jcode: durable daemon, context, and task DAG

Best parts to adapt:

- a detached daemon serving reconnectable clients;
- snapshot plus append-only journal recovery;
- centralized tool lifecycle and safe interruption semantics;
- tiered background/emergency compaction that preserves tool pairs;
- optional one-turn-late memory retrieval through a bounded nonblocking queue;
- a task DAG with typed dependency artifacts, worker ownership, critique gates,
  and explicit `what_i_did_not_check`; and
- cross-harness session import.

Do not copy unchanged:

- host `bash -c` execution without a general sandbox;
- fail-open pre-tool hook failures;
- shared MCP as a permissive default or full environment inheritance;
- incomplete permission handling in the public harness API;
- optimistic file-conflict notifications without isolation or leases;
- recursive modes without comprehensive depth/cost/RAM bounds; or
- opt-out telemetry and sponsored discovery defaults.

Sources:
[server architecture](https://github.com/1jehuang/jcode/blob/11cda7dcbc4d685589d96a0af26af963be751352/docs/SERVER_ARCHITECTURE.md),
[session persistence](https://github.com/1jehuang/jcode/blob/11cda7dcbc4d685589d96a0af26af963be751352/crates/jcode-base/src/session/persistence.rs),
[compaction core](https://github.com/1jehuang/jcode/blob/11cda7dcbc4d685589d96a0af26af963be751352/crates/jcode-compaction-core/src/lib.rs),
[memory agent](https://github.com/1jehuang/jcode/blob/11cda7dcbc4d685589d96a0af26af963be751352/crates/jcode-base/src/memory_agent.rs),
[task DAG](https://github.com/1jehuang/jcode/blob/11cda7dcbc4d685589d96a0af26af963be751352/docs/SWARM_TASK_GRAPH.md),
[shell tool](https://github.com/1jehuang/jcode/blob/11cda7dcbc4d685589d96a0af26af963be751352/crates/jcode-app-core/src/tool/bash.rs),
[hook policy](https://github.com/1jehuang/jcode/blob/11cda7dcbc4d685589d96a0af26af963be751352/docs/HOOKS.md),
[MCP client](https://github.com/1jehuang/jcode/blob/11cda7dcbc4d685589d96a0af26af963be751352/crates/jcode-base/src/mcp/client.rs),
[harness API status](https://github.com/1jehuang/jcode/blob/11cda7dcbc4d685589d96a0af26af963be751352/docs/HARNESS_API_AND_DESKTOP_REWRITE.md),
[telemetry policy](https://github.com/1jehuang/jcode/blob/11cda7dcbc4d685589d96a0af26af963be751352/TELEMETRY.md),
and [sponsor configuration](https://github.com/1jehuang/jcode/blob/11cda7dcbc4d685589d96a0af26af963be751352/crates/jcode-config-types/src/lib.rs).

### Claude Code: independently specified interaction contracts

Behaviors to specify and then independently implement:

- a gather-context, act, and verify loop;
- hierarchical project instructions and explicit memory;
- scoped subagents with separate context and tool rules;
- typed lifecycle hooks;
- session resume, fork, compact, and checkpoint UX;
- agent teams with a shared task list and messaging;
- isolated worktrees; and
- deferred MCP tool discovery for large catalogs.

These are behavioral requirements only. The public Claude Code repository does
not provide a permissively licensed implementation of the core. Before coding,
one review produces a neutral behavior specification; implementers trace only
that specification and approved permissive sources. Legal review covers terms,
patents, trademarks, provenance, and whether stronger separation is required.

Sources:
[how Claude Code works](https://code.claude.com/docs/en/how-claude-code-works),
[hooks](https://code.claude.com/docs/en/hooks),
[subagents](https://code.claude.com/docs/en/sub-agents),
[agent teams](https://code.claude.com/docs/en/agent-teams),
[sessions](https://code.claude.com/docs/en/sessions),
[checkpointing](https://code.claude.com/docs/en/checkpointing), and
[repository license](https://github.com/anthropics/claude-code/blob/7ef6eec9d9ba84ea6f233f26c45f1df5c5991843/LICENSE.md).

### Orqen: durable orchestration, not the executor

Best audited concepts to adapt after permission:

- immutable graph versions plus topology/orchestration validation;
- idempotency fingerprints, worker leases, fencing generations, cancellation,
  monotonic sequences, and bounded resumable streams;
- deterministic bounded execution waves with durable CAS checkpoints and
  unapplied-result recovery;
- provider-neutral request resolution and verified tool schemas; and
- optimistic shared state with explicit version checks.

Do not use Orqen as the kernel. It has no filesystem, patch, shell, PTY, Git,
LSP, worktree, or local OS-sandbox primitive. Its MCP path is static remote
HTTP, context summaries can grow monotonically, each delta is committed
individually, and observability can retain full tool inputs/results.

Sources: [graph compiler](../../../Orqen/server/app/features/agent_graphs/runtime/multi_agent_plan.py),
[durable scheduler](../../../Orqen/server/app/features/agent_graphs/runtime/multi_agent_scheduler.py),
[leases](../../../Orqen/server/app/features/agent_graphs/runs/queue.py), and
[provider contract](../../../Orqen/server/app/features/model_providers/adapters/base.py).

The public tool-selection and cache claims remain hypotheses: measure recall,
quality, cost, latency, reconstruction accuracy, and safe source restoration.

### documentIntelligence: evidence-grounded document adapter

Best audited concepts to adapt after permission:

- typed agent definitions separated from reusable execution drivers;
- canonical versioned event envelopes and bounded/redacted payloads;
- per-tool concurrency, timeout, cancellation, and safe failure contracts;
- opaque run-local source IDs, candidate/source-query tracking, relevant versus
  rejected evidence, and grounded finish validation; and
- artifact versions, replay reducers, and reconnectable client state.

Harden before use. Permission helpers are not wired into the tool loop,
checkpoints have no restore path, and persistence failure silently disables
recording. Tenant choice can be implicit; ingestion uses process-local tasks,
full-buffer reads, host parsers, weak upload validation, and no ANN index.
Model-authored evidence is not verified as an exact source substring.

Sources: [runtime definition](../../../documentIntelligence/server/app/services/agents/runtime/definition.py),
[event envelope](../../../documentIntelligence/server/app/services/agents/runtime/events.py),
[tool runtime](../../../documentIntelligence/server/app/services/agents/runtime/support/tools.py),
and [finish validation](../../../documentIntelligence/server/app/services/agents/definitions/corpus_search/finish_validation.py).

## 5. Source-to-design traceability

| Atlas decision | Base or inspiration | Why it survives synthesis |
| --- | --- | --- |
| Python control plane and Pydantic protocol | Existing repository plus Codex behavior evidence | Fastest integrated path while preserving strict process boundaries |
| OS sandbox plus allow/ask/deny policy | Codex, tightened | Permissions alone are not containment |
| Event journal with atomic projections | OpenCode | Makes state replay deterministic and external ambiguity explicit |
| Persistent local daemon | Jcode plus Codex app server | Decouples client and agent lifetimes |
| Capability-split provider adapters | OpenCode and Jcode, redesigned | Prevents a monolithic provider trait and silent feature loss |
| Hot/warm/cold context with validation | Jcode, OpenCode, measurable Orqen claims | Controls cost without silently losing constraints |
| Validated bounded DAG, leases, typed handoffs | Orqen, Jcode, Codex multi-agent | Adds durable orchestration without granting executor authority |
| Scoped subagents, hooks, checkpoints, worktrees | Codex plus independently reviewed Claude Code behavior | Strong user control and recoverable parallel work |
| Progressive tool discovery | OpenCode, Claude Code docs, public Orqen | Avoids injecting every large schema while preserving recall tests |
| Out-of-process extensions | New hardening decision | Shrinks trusted computing base |
| Canonical document events and evidence ledger | documentIntelligence, redesigned | Grounds retrieval while strengthening tenancy, persistence, and citations |

## 6. Licensing and provenance

| Source | Snapshot license evidence | Allowed treatment in Atlas |
| --- | --- | --- |
| Codex | [Apache-2.0](https://github.com/openai/codex/blob/4c43465133428898aa84f0bfc02c306ed65fb66a/LICENSE) | Behavior/security evidence now; future adaptation only through the deferred Rust activation and file-level review |
| OpenCode | [MIT](https://github.com/anomalyco/opencode/blob/7534d23551f665e65080809975b4ca5c7d63807b/LICENSE) | Adapt selected code with copyright/license retention |
| Jcode | [MIT](https://github.com/1jehuang/jcode/blob/11cda7dcbc4d685589d96a0af26af963be751352/LICENSE) | Adapt selected code with copyright/license retention |
| Claude Code | [All rights reserved](https://github.com/anthropics/claude-code/blob/7ef6eec9d9ba84ea6f233f26c45f1df5c5991843/LICENSE.md) | Independent behavior specification and legal review only |
| Public Orqen page | No source license established | Test product-level ideas; copy no implementation |
| Local Orqen | Audited tree; no `LICENSE`, package license, or notice | Architecture reference only until written permission |
| Local documentIntelligence | Audited tree; no `LICENSE`, package license, or notice | Architecture reference only until written permission |

Maintain a machine-readable provenance manifest for every adapted file,
including upstream repository, commit, original path, license, local changes,
and update owner. Run license and dependency scanning in CI, but keep legal
approval as a human release gate.

P0 also chooses Atlas’s compatible outbound license and defines SPDX headers,
bundled LICENSE/NOTICE and attribution contents, and release-package checks.

## 7. Why a selective hybrid beats every whole-codebase option

A direct Codex fork would introduce Rust and a second application/runtime stack
before the current product contracts are stable. A direct OpenCode or Jcode fork
would require retrofitting a consistent OS isolation boundary. Claude Code
cannot supply a reusable core under the observed license. Orqen and
documentIntelligence pass the source-evidence gate but still fail the license
and coding-sandbox gates.

The selective design therefore keeps one authoritative turn state machine, one
event vocabulary, one policy gateway, one sandbox supervisor, and one durable
store. Other projects contribute replaceable adapters and algorithms around
that kernel. This avoids the failure mode of “best of everything” systems:
multiple overlapping orchestrators whose permission, cancellation, persistence,
and retry semantics disagree.

## 8. Risks and mitigations

| Risk | Mitigation and release evidence |
| --- | --- |
| Python event-loop blocking or GC increases latency | Bounded async ownership, blocking-worker isolation, latency/RSS benchmarks, and Rust activation thresholds |
| Adapted MIT code creates provenance ambiguity | File-level manifest, retained notices, code review label, SBOM |
| Context optimization reduces task quality | Paired on/off evaluations, critical-fact validator, source-span restoration |
| Provider normalization hides semantics | Capability traits, lossless provider metadata, conformance matrix |
| Multi-agent use explodes cost or conflicts | Hard depth/fan-out/concurrency/cost limits, leases/overlays, independent verification |
| SQLite becomes writer bottleneck | Batch deltas, benchmark thresholds, stable event contract for later partitioning |
| Extension compromises workspace or secrets | Out-of-process host, default-deny capability, filtered env, sandbox, network proxy |
| Approval fatigue causes unsafe broad rules | Narrow suggested scopes, expiry, deny precedence, receipts, policy lint |
| Local-source license remains unresolved | Copy no code; retain concept-level specification and provenance review |
| “Best” claim becomes marketing | Publish pinned tasks, raw failures, latency, cost, tokens, RSS, and confidence intervals |

## 9. Falsifiable success criteria

Atlas earns the “best base harness” label only after a pinned release candidate:

- blocks every case in the approved escape and secret-exfiltration suites;
- loses no acknowledged event, automatically retries only idempotent or
  reconciled operations, and halts ambiguous side effects;
- keeps process RSS, queues, subscribers, jobs, memory, and storage within
  configured bounds during a 24-hour soak;
- preserves all critical constraints in long-context and compaction fixtures;
- meets or exceeds the strongest reviewed baseline on a statistically useful
  coding-task suite without hiding model, token, cost, or failure differences;
- supports provider replacement without changing core turn semantics;
- recovers CLI, IDE, and SDK clients from disconnect using the same sequence;
- bounds multi-agent depth, concurrency, cost, file conflicts, and cancellation;
- passes independent security and license review; and
- documents failures and regressions as prominently as wins.

Until those gates pass, the accurate claim is: **the existing Python platform
is the lowest-risk current implementation base, and Atlas remains a proposed
best-of-breed design.**

## 10. Local-source licensing and adaptation gate

The read-only module, security, concurrency, persistence, and feature audits are
complete. Before implementation uses either local tree:

1. record owner, immutable revision/export hash, and written license grant;
2. map every adapted concept or file to provenance, license, local changes, and
   maintenance owner;
3. independently reimplement anything not expressly licensed;
4. run dependency, tenant-isolation, parser, SSRF, secret, persistence,
   cancellation, and recovery tests; and
5. issue an ADR for any proposal that changes the Python process boundary.

Until this gate closes, the plan may name evidenced behavior, but production
source comes only from this repository, approved permissive projects with
file-level provenance, or new independent implementation.
