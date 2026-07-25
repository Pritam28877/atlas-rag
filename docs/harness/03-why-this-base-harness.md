# Why Codex is the best base for Atlas Harness

Status: proposed architecture decision, researched on 2026-07-25.

## Decision

Use the Apache-2.0
[OpenAI Codex repository](https://github.com/openai/codex/tree/4c43465133428898aa84f0bfc02c306ed65fb66a)
as the base kernel. Extend it through a small set of upstream-compatible Rust
crates and adapters. Selectively adapt MIT-licensed patterns from OpenCode and
Jcode. Independently implement selected Claude Code behavior. The locally
audited Orqen and documentIntelligence trees provide concrete architectural
evidence, but neither exposes a license grant; copy none of their source until
ownership and permission are recorded. Calling work “clean room” does not by
itself settle legal obligations.

This is the best **base**, not a claim that Codex already has every desired
feature. Its advantage is that the hardest-to-retrofit foundations—typed agent
state, a structured app-server protocol, OS-level sandboxing, executable policy,
session recovery, multi-agent support, and an actively tested Rust core—are
available under a permissive license.

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
7. **Maintainability:** the fork surface can stay small enough to receive
   upstream security and compatibility fixes.

After hard gates, compare provider portability, context quality, multi-agent
coordination, client experience, performance, and operational maturity. A
marketing benchmark, star count, or feature count cannot override a failed hard
gate.

## 2. Gate comparison

| Candidate | Reusable source/license | OS execution boundary | Durable typed control protocol | Direct-base result |
| --- | --- | --- | --- | --- |
| Codex | Yes, Apache-2.0 | Linux sandbox and executable policy are source-visible, but privileged app-server bypasses must be removed | App server models Thread, Turn, Item, streaming events, resume/fork/compact | **Conditional pass; selected kernel after hardening** |
| OpenCode | Yes, MIT | Its own security policy says it is not a sandbox; host tools and in-process plugins remain trusted | Strong server/SDK and new event journal, but legacy and new stacks coexist | Conditional; adapt modules, not whole base |
| Jcode | Yes, MIT | Normal shell/tool paths do not provide a general OS sandbox; some hook failures are fail-open | Strong daemon/session design; stable API bridge is incomplete | Conditional; adapt daemon, DAG, and compaction patterns |
| Claude Code | Public behavior docs; repository license is all-rights-reserved | Documented Seatbelt/bubblewrap sandbox and permissions | Documented sessions, hooks, agents, teams, worktrees, checkpoints | Fail legal-source gate; independent behavior specification only |
| Local Orqen | Source audited; no visible license | No coding-tool or OS-sandbox kernel | Strong versioned graphs, idempotent leased runs, deterministic scheduler, recovery, providers, replay | Fail legal/security base gates; adapt concepts only after permission |
| Local documentIntelligence | Source audited; no visible license | No coding sandbox; host parsers need isolation | Typed runtime/events, bounded tool-loop patterns, artifacts, grounded corpus workflow; permissions/resume incomplete | Fail direct-base gates; adapt document/runtime contracts after permission |

The public repositories are pinned; the two local working trees were reviewed
read-only on 2026-07-25 and are not treated as redistributable source.

## 3. Why Codex wins the base decision

### Security is already architectural

Codex separates user policy from OS enforcement. Its Linux sandbox uses
bubblewrap and seccomp-related isolation, while executable policy can classify
command prefixes as allowed, prompt-required, or forbidden. That is a much
stronger starting point than adding a permission dialog around unrestricted
host execution.

The proposed harness still tightens the base: isolation must fail closed when
required controls are unavailable; network and secrets need capability
brokering; all extension processes need the same boundary; and macOS/Windows
must pass equivalent escape suites.

Codex is not safe to fork unchanged. The pinned app server documents
`thread/shellCommand` and `process/spawn` as unsandboxed and exposes direct host
filesystem RPCs. Atlas disables those methods until each path passes actor
authorization, policy, sandbox, output filtering, and audit. A release gate
inventories all privileged RPCs and rejects newly reachable bypasses.

Sources:
[Linux sandbox](https://github.com/openai/codex/blob/4c43465133428898aa84f0bfc02c306ed65fb66a/codex-rs/linux-sandbox/README.md),
[executable policy](https://github.com/openai/codex/blob/4c43465133428898aa84f0bfc02c306ed65fb66a/codex-rs/execpolicy/README.md),
[app-server privileged methods](https://github.com/openai/codex/blob/4c43465133428898aa84f0bfc02c306ed65fb66a/codex-rs/app-server/README.md),
and [Codex security documentation](https://developers.openai.com/codex/security/).

### The protocol is a real product boundary

The Codex app server exposes machine-readable Thread, Turn, and Item state with
streamed notifications and schema generation. It supports durable lifecycle
operations such as resume, fork, and compact, plus approvals, tools, plans,
goals, and multi-agent events. Bounded queues and backpressure are considered in
the server design.

That boundary lets a CLI, TUI, IDE, desktop app, automation client, and future
remote control plane share one semantic model. Atlas can add event sourcing and
provider abstraction underneath without making UI state authoritative.

Source:
[Codex app-server README](https://github.com/openai/codex/blob/4c43465133428898aa84f0bfc02c306ed65fb66a/codex-rs/app-server/README.md).

### Rust fits the trusted kernel

The policy engine, protocol parser, session state machine, sandbox supervisor,
streaming adapters, and scheduler form a security- and concurrency-sensitive
kernel. Rust provides memory safety without a garbage collector, explicit
ownership for subprocess and cancellation lifecycles, and a mature async stack.
This does not guarantee correctness, but it reduces the classes of failure in
the most trusted code.

TypeScript remains appropriate for generated SDKs and clients. Untrusted
JavaScript plugins move out of process instead of expanding the kernel’s trusted
computing base.

### The extension model is broad without forcing one UI

Codex already has instruction discovery, skills, plugins, MCP, hooks,
customizable agents, provider configuration, and multi-agent operation. The
proposed design preserves those concepts while routing every extension through
one capability and budget gateway.

Sources:
[multi-agent documentation](https://developers.openai.com/codex/multi-agent/),
[AGENTS.md guidance](https://developers.openai.com/codex/guides/agents-md/),
[skills](https://developers.openai.com/codex/skills/), and
[MCP](https://developers.openai.com/codex/mcp/).

### A measured derivative can stay maintainable

Codex is actively developed. A wholesale merge of several fast-moving
repositories would create permanent conflict, duplicated state machines,
incompatible event vocabularies, and an unreviewable security boundary. The
target is a thin derivative, but P0 must prove it by mapping mutation points,
privileged RPCs, and a maintained upstream-merge budget. Compatibility adapters
then isolate borrowed ideas from the kernel.

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
| Rust trusted kernel and app-server semantics | Codex | Strongest licensable security/protocol foundation |
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
| Codex | [Apache-2.0](https://github.com/openai/codex/blob/4c43465133428898aa84f0bfc02c306ed65fb66a/LICENSE) | Fork/adapt with notices, license, and change records |
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

A direct Codex fork lacks some desired provider, event-sourcing, DAG, and
context-optimization features. A direct OpenCode or Jcode fork would require
retrofitting the most security-sensitive property: a consistent OS isolation
boundary. Claude Code cannot supply a reusable core under the observed license.
Orqen and documentIntelligence now pass the source-evidence gate but still fail
the license and coding-sandbox gates.

The selective design therefore keeps one authoritative turn state machine, one
event vocabulary, one policy gateway, one sandbox supervisor, and one durable
store. Other projects contribute replaceable adapters and algorithms around
that kernel. This avoids the failure mode of “best of everything” systems:
multiple overlapping orchestrators whose permission, cancellation, persistence,
and retry semantics disagree.

## 8. Risks and mitigations

| Risk | Mitigation and release evidence |
| --- | --- |
| Upstream Codex changes faster than Atlas | P0 mutation/RPC map and merge budget, automated compatibility suite, scheduled security rebase |
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

Until those gates pass, the accurate claim is: **Codex is the strongest
evidence-backed base among the reviewed candidates, and Atlas is the proposed
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
5. issue an ADR for any proposal that changes the Codex kernel boundary.

Until this gate closes, the plan may name evidenced behavior, but production
source comes only from Codex, approved permissive projects, or new independent
implementation.
