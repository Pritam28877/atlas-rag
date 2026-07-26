# Atlas Harness: end-to-end implementation plan

Status: Python-first proposed delivery plan, revised on 2026-07-26.

This plan turns the [architecture and data-flow contract](01-best-of-breed-architecture.md)
into a production-grade coding-agent harness. “Best” means safe enforcement,
durable execution, provider portability, bounded agents, and measured quality;
it does not mean combining every feature from every reference project.

## 1. Outcome and definition of done

The first production release is done only when a user can:

1. create or resume a durable thread from CLI, TUI, IDE, or generated SDK;
2. run the same typed turn loop against at least two cloud providers and one
   OpenAI-compatible local endpoint;
3. inspect why a provider, tool, permission, and context item was selected;
4. approve a narrowly scoped action without weakening the OS sandbox;
5. disconnect and reconnect without losing acknowledged events;
6. cancel a turn and prove that owned processes and requests were contained;
7. delegate a bounded task DAG to isolated workers with typed evidence;
8. recover control state after daemon termination, retry only idempotent or
   reconcilable work, and halt any ambiguous external side effect;
9. export a redacted trace and replay it against a mock provider; and
10. pass the security, reliability, quality, latency, cost, and memory gates in
    this document.

No production claim is allowed from compilation or vendor benchmarks alone.

## 2. Scope and non-goals

### In scope

- Python 3.12 feature modules in the uv-managed FastAPI application, with domain logic independent of FastAPI and provider SDKs.
- Local JSON-RPC over stdio/Unix socket; authenticated loopback HTTP as needed.
- SQLAlchemy event sessions with PostgreSQL tests, local SQLite, and content-addressed local/S3 blobs.
- Capability-split provider layer and canonical streamed events.
- Policy, approval, budget, sandbox, secret, and audit enforcement.
- Tools, MCP, isolated plugins, hooks, skills, and instructions through one boundary.
- Hot/warm/cold context, safe compaction, progressive discovery, bounded memory.
- Bounded task DAG, child sessions, per-agent overlays, and verification gates.
- CLI/TUI, generated TypeScript SDK, IDE adapter, observability, and evaluation.
- An isolated evidence-ledger document adapter with immutable scoped citations.
- A separate deferred Rust migration plan that is not part of this release.

### Non-goals for the first release

- No distributed multi-host scheduler.
- No autonomous branch, commit, push, deployment, billing, or account creation.
- No in-process third-party plugins.
- No arbitrary remote control-plane binding without authentication and TLS.
- No unbounded transcript, vector memory, log, queue, retry, or agent recursion.
- No direct Orqen or documentIntelligence source reuse without a recorded
  license grant; their current local trees are architecture references only.
- No model downgrade or context deletion hidden from the user.
- No Rust build input or runtime component in the current release.

## 3. Repository and module layout

Keep each Python module feature-focused and expose narrow protocols:

```text
app/services/harness/
  protocol/ + events/ + runtime/ + sessions/
  journal/ + artifacts/ + providers/ + context/
  policy/ + sandbox/ + tools/ + extensions/
  scheduler/ + memory/ + documents/
  observability/ + evaluation/
app/api/v1/harness/                    authenticated HTTP/stream boundary
app/cli/harness/                       CLI and keyboard-accessible TUI
sdk/typescript/ + schemas/harness/     generated SDK and schemas
tests/harness/                          contract and E2E verification
```

The Python control plane may execute only validated domain and coordination code
in process. Model-authored content, tools, MCP servers, plugins, hooks, parsers,
and code interpreters run in owned, filtered, resource-bounded subprocesses or
workers behind the same policy and sandbox-supervisor contract. The
[deferred Rust plan](07-rust-deferred-implementation-plan.md) can start only
after the Python protocol is frozen, bottlenecks are measured, and the user
explicitly approves its activation gate.

## 4. Canonical data model

| Aggregate or record | Required fields and invariant |
| --- | --- |
| Principal | Server-derived actor ID, authentication method, issuer, session binding; never trusted from command payloads |
| Grant | Principal, workspace, roles/capabilities, policy version, issued/expiry times, revocation state |
| Workspace | ID, owner/tenant, canonical root, repository fingerprint, policy version; roots are never inferred from model output |
| Thread | ID, workspace ID, parent/fork origin, status, retention class; ordered by aggregate event sequence |
| Turn | ID, thread ID, idempotency key, requested agent/model policy, state, deadlines, token/cost budget |
| Item | Typed user, assistant, reasoning, tool call, tool result, approval, artifact, or error payload |
| Event | Event ID, aggregate ID, sequence, schema version, actor/grant decision IDs, timestamp, causation/correlation IDs, payload hash |
| ContextManifest | Ordered source references, exact hashes, token counts, cache boundary, omitted-reason list |
| ProviderDecision | Requirements, eligible routes, selected route, rejection reasons, health snapshot, price version |
| Operation | Stable ID/idempotency key, attempt, lease fencing token, tool/version, args hash, capability, `Prepared/Dispatched/Completed/Failed/Ambiguous`, limits |
| ApprovalReceipt | Request hash, subject, scope, decision, expiry, policy version; append-only and redacted |
| TaskNode | Parent, dependencies, owner, state, priority, depth, budgets, file lease or overlay |
| HandoffArtifact | Claims, evidence references, changed artifacts, verification, risks, `not_checked` list |
| MemoryFact | Fact, source event IDs, confidence, namespace, created/verified/expiry times; never authority |
| EvaluationRun | Harness/model/config revisions, fixture hashes, scores, latency, cost, failures, trace references |

Large tool outputs and file snapshots live in content-addressed blob storage.
Events contain bounded metadata and hashes, not duplicated megabyte payloads.
Secrets are handles resolved only at the execution boundary.

## 5. Protocol and compatibility

### Commands

Start with a versioned envelope containing `request_id`, `client_id`,
`workspace_id`, optional `expected_sequence`, and one typed command:

- workspace open/close;
- thread create/resume/fork/archive/list;
- turn start/steer/cancel/compact;
- approval respond;
- task inspect/cancel/retry;
- model/tool/MCP capability list;
- event subscribe/acknowledge;
- artifact fetch with ranges; and
- evaluation start/status.

Mutating commands require idempotency keys. List calls are cursor-paginated.
Clients may request events after a durable sequence number. Slow subscribers
receive a resync marker and catch up from the SQL journal instead of growing an
unbounded memory queue.

The server binds every command to its authenticated `Principal` and current
`Grant`; `client_id`, `workspace_id`, and object IDs are selectors, never proof
of authority. Every event subscription and approval response is re-authorized.

### Events

Use one canonical vocabulary for accepted, started, delta, requested, approved,
denied, completed, failed, cancelled, compacted, retried, task, artifact, usage,
and recovery events. Preserve unknown fields for compatible clients. Generate
JSON Schema and TypeScript from strict Pydantic v2 source models. Test both directions: new
readers consume the previous two minor versions, while rollback readers preserve
unknown fields and event types without projecting or discarding them. A writer
version gate blocks schemas that the declared rollback binary cannot retain.

### Transport

1. stdio for a client-owned daemon, bound to the spawning process identity;
2. Unix socket peer credentials and mode, or Windows named-pipe ACL, plus a
   short-lived session token for a persistent local daemon;
3. authenticated loopback HTTP/SSE or WebSocket with strict Host/Origin checks,
   CSRF protection, and non-cookie bearer binding for IDE/browser clients; and
4. remote mutual TLS only after tenant isolation, origin protection, rate limits,
   audit retention, and operational ownership are approved.

## 6. End-to-end turn data flow

1. The app server authenticates the principal, resolves and authorizes the exact
   workspace, checks request size, and deduplicates the turn idempotency key.
2. It appends `TurnAccepted` transactionally with the thread sequence and
   projects the new state.
3. The session coordinator acquires or joins the single-writer lease. A bounded
   wake signal starts one run; additional wakes coalesce.
4. The context compiler creates an immutable manifest from policy, agent
   instructions, the recent paired tail, task artifacts, summaries, retrieved
   facts, and a relevance-filtered tool catalog.
5. Model-aware tokenization reserves output, reasoning, and tool-result space.
   If the request cannot fit without losing hard constraints, the turn pauses
   with an explicit context error.
6. The provider router filters by required modalities, tool calling, context
   size, data policy, region, credentials, health, and budget. It records every
   eliminated route and the final decision.
7. The policy/egress gateway authorizes provider, destination, data class, cost,
   and credential handle, records `ProviderAttemptStarted`, then the adapter
   streams normalized events through a bounded channel. Deltas are batched
   before persistence and authorized client broadcast.
8. The turn engine validates each tool call against its registered schema and
   creates a stable operation ID, idempotency class, and normalized args hash.
9. The policy gateway intersects system, organization, workspace, agent, skill,
   plugin, and user-granted capabilities. The most restrictive rule wins.
10. A denied operation becomes a model-visible structured result. An
    approval-required operation pauses durably and streams a bounded request.
11. Before dispatch, acquire a fencing lease, append synced `OperationPrepared`
    with its token, then append `OperationDispatched`. The sandbox receives a
    filtered environment, workspace view, network policy, limits, deadline,
    cancellation token, and output cap.
12. Raw output passes through validation, redaction, and blob limits before a
    paired `Completed`, `Failed`, or `Ambiguous` event is stored. Ambiguous work
    is never automatically retried without tool-supported status reconciliation.
13. Steps 7–12 repeat until final output, cancellation, terminal failure, or any
    step/token/cost/time/repeated-call budget is exhausted.
14. `TurnCompleted` is appended only after final usage and referenced artifacts
    are durable. Asynchronous memory extraction and evaluation use bounded
    queues and cannot change the completed answer.

## 7. Provider subsystem

Split the broad provider abstraction into independently testable capabilities:

- `InferenceTransport`: request, stream, cancellation, deadlines, retry hints;
- `ModelCatalog`: models, revisions, context and output limits, modalities;
- `CredentialSource`: opaque handles and refresh lifecycle;
- `RequestCompiler`: canonical messages/tools to provider wire format;
- `StreamDecoder`: wire events to canonical events;
- `ContextCapability`: native compaction or cache semantics;
- `UsageAndPrice`: token categories, price revision, estimated/final cost; and
- `DataPolicy`: residency, retention, egress, and training constraints.

Retries apply only to classified transient failures, use jitter, respect
provider retry hints, and share a turn-wide attempt and wall-time budget.
Failover requires equivalent capabilities and explicit policy permission.
Never retry an ambiguous side effect through the model loop.

Provider conformance uses recorded, redacted fixtures for text, reasoning, tool
calls, parallel calls, malformed streams, rate limits, context overflow,
cancellation, and usage accounting. Live tests require explicitly configured
credentials and cost ceilings.

All provider traffic uses the same egress gateway for credential resolution,
destination allowlisting, data-loss checks, request IDs, cost reservation,
response limits, and audit. Telemetry export uses an equivalent redaction and
egress gate; neither subsystem connects directly from the journal or router.

Orqen demonstrates provider normalization but is not a drop-in package.
Independently implement OpenAI, OpenRouter, Bedrock, Vertex/Gemini, and local
adapters under one lifecycle and conformance contract.

## 8. Policy, approval, and sandbox subsystem

### Effective authorization

Represent capabilities as structured values, not shell-string guesses:

- filesystem roots and read/write operations;
- executable plus parsed argument constraints;
- network host, port, method, and redirect policy;
- secret handle and destination binding;
- tool/MCP/plugin identity and version;
- maximum process, CPU, memory, output, and wall time; and
- allowed child-agent role, depth, and budget.

Compile rules to `allow`, `ask`, or `deny`; `deny` always wins. Approval may be
once, session-scoped, workspace-scoped with expiry, or a proposed configuration
change. Record the evaluated rule set and request hash so a mutated operation
cannot reuse an approval.

### Isolation

- Linux: bubblewrap namespaces, seccomp, cgroups v2 where available, read-only
  host mounts, explicit writable roots, and proxy-mediated network.
- macOS: Seatbelt profiles plus process and output limits.
- Windows: restricted token, Job Object, ACL-constrained workspace, and network
  containment where supported.
- If required isolation is unavailable, fail closed or start an explicitly
  labeled read-only mode; never silently execute unsandboxed.

MCP and plugin processes default to per-workspace isolation, receive an
allowlisted environment, have bounded pending maps, and are terminated on
deadline or workspace close. Sharing requires a stateless declaration and
security review.

The pinned Codex review shows why raw shell, process, and host-filesystem
methods are bypass risks. Atlas exposes no equivalent Python route, worker task,
tool, hook, parser, plugin, or provider path until authentication, policy,
sandbox or egress, result filtering, and audit are enforced. A CI-readable
registry inventories every privileged operation and rejects missing evidence.

## 9. Context, compaction, and memory

### Tiers

- Hot: current request, active constraints, and recent complete tool pairs.
- Warm: anchored summaries, active plan, verified decisions, and selected task
  artifacts.
- Cold: retained event history and immutable blobs, fetched only by reference.

Use model-aware token counts and stable prefix hashes. Summarize only at safe
turn boundaries. Each summary stores covered event ranges, source hashes, a
critical-fact checklist, and the summarizer revision. Validate identifiers,
paths, URLs, acceptance criteria, user prohibitions, unresolved errors, and tool
schemas after compaction. Restore source spans if validation fails.

Optional memory is namespaced by user/workspace, capped by entries and bytes,
expires by policy, and carries provenance. Retrieval is bounded by count,
bytes, and deadline. It may suggest context but cannot grant permissions or
override current instructions.

## 10. Tools, hooks, MCP, plugins, and document intelligence

One tool registry owns schema validation, aliases, versioning, policy lookup,
execution, output normalization, truncation, tracing, and model-visible errors.
Long-running commands can become durable background jobs only when they expose
status, cancellation, TTL, output bounds, and ownership.

Only the typed hook dispatcher is trusted. Third-party command, HTTP, MCP, and
prompt hooks execute in isolated hosts through the same policy, egress, secret,
budget, and output gates as tools. Authorization and security gates fail closed;
advisory-hook failure behavior is explicit. Notification hooks cannot mutate
execution.

Large tool catalogs use progressive discovery: send compact metadata first,
then load full schemas for selected tools. Any code-mode interpreter must avoid
`eval`, validate its AST, and enforce execution-step, depth, call-count,
concurrency, time, and output limits.

The document-intelligence boundary accepts artifact IDs, tenant/workspace scope,
query or parsing intent, and hard limits. It returns typed chunks, provenance,
confidence, and unsupported outcomes. Adapt opaque run-local source IDs,
candidate scores/source queries, relevant/rejected classification, and a finish
gate that rejects unknown or unclassified evidence. Every citation binds an
artifact/version, SHA-256, span, retriever revision, and tool-call ID; quoted
evidence must match its source. Tenant scope is explicit, parsing is isolated,
blobs stream, and persistence failure stops the run. The local code is not
copied because it lacks these guarantees and a visible license grant.

## 11. Multi-agent orchestration

The planner validates a versioned graph and rejects cycles, invalid joins,
unreachable nodes, and budget violations. Persist its immutable hash. A durable
scheduler leases nodes in deterministic bounded waves and checkpoints applied
results with compare-and-set versioning. Each worker receives:

- a child session with its own context and capability intersection;
- a declared task and token/cost/time budget;
- a worktree, overlay, or file lease;
- only dependency artifacts required for its task; and
- a required handoff schema including what was not checked.

Workers cannot recursively spawn unless their role permits it. Enforce maximum
depth, per-node fan-out, total agents, runnable queue length, provider
concurrency, and project cost. Verification nodes are independent of producer
nodes. Shared-file conflicts stage patches for deterministic resolution instead
of relying only on agent messages. This retains Orqen's strongest audited
pattern—graph validation, lease generations, durable checkpoints, and ordered
recovery—behind the Atlas-owned Python executor capability interface.

### Initial enforced budgets

| Resource | Default | Initial hard maximum | Overflow behavior |
| --- | --- | --- | --- |
| Active sessions / daemon | 8 | 64 | Queue within admission deadline, then reject |
| Provider calls / workspace | 4 active, 128 queued | 16 active, 512 queued | Reject newest with retry metadata |
| Agents / depth / DAG nodes | 4 / 2 / 128 | 16 / 4 / 1,024 | Pause parent and persist budget error |
| Turn steps / provider retries / identical calls | 64 / 2 / 3 | 256 / 5 / 5 | Terminal budget result |
| Subscriber / stream channel | 256 / 256 events | 2,048 / 2,048 | Coalesce deltas, then durable resync |
| Pending MCP calls / server | 32 | 256 | Reject new call and preserve cancellation |
| Tool wall time / output stream | 10 min / 1 MiB | 1 hour / 16 MiB | Terminate or truncate with explicit event |
| Memory facts / bytes / workspace | 10,000 / 64 MiB | 100,000 / 1 GiB | TTL then LRU eviction |
| Retained events / blobs / workspace | 90 days / 2 GiB | 64 GiB plus disk reserve | Seal read-only before reserve is crossed |
| Cloud spend | Disabled until user sets a cap | Signed organization grant | Stop before dispatch; no retry reset |

These are release-candidate values to benchmark, not universal constants.
Operators may lower them; raising a hard maximum requires reviewed policy and
new soak evidence. Queued and running work both count. Retries, reconnects, and
resumes do not reset budgets, and every child consumes its parent’s allocation.

## 12. Persistence, recovery, and retention

Append an event and update its projection in one SQLAlchemy transaction. Use
PostgreSQL row locking and constraints in production-like environments,
explicit local SQLite semantics, monotonic per-aggregate sequences, and
compare-and-set expected versions.
Content-address large payloads and verify hashes on read.

Durability classes:

- interactive deltas may batch on a short interval;
- completed items, task handoffs, and session metadata require durable commit;
- approvals, secret access, and non-idempotent operation receipts require the
  strongest available synced commit.

On startup, verify schema, truncate no data automatically, rebuild disposable
projections when necessary, reconcile running operations, expire leases, and
mark ambiguous non-idempotent work `NeedsOperator`. Retention caps session age,
event count, blob bytes, memory entries, terminal jobs, and exported telemetry.

Append-only means immutable within an active or sealed segment, not retention
forever. A synced snapshot establishes a replay baseline before whole sealed
segments can be archived or policy-deleted. Approval/audit records follow their
separate retention class. Deletion tombstones retrieval immediately; blob
garbage collection runs only after reachability, legal-hold, and grace checks.

## 13. Observability and evaluation

Every trace uses thread, turn, task, provider-decision, operation, and event IDs.
Central redaction runs before logs, metrics labels, traces, crash reports, and
exports. Never emit prompts, file bodies, tool output, tokens, cookies, headers,
credentials, or user identifiers by default.

Minimum metrics:

- queue depth/age, active sessions/agents/processes, dropped wakeups;
- turn time-to-first-event and completion latency;
- provider attempts, failures, cancellation latency, tokens, and cost;
- tool approval/denial/failure, sandbox violation, output truncation;
- context composition, cache hit, compaction validation, retrieval latency;
- event append latency, database size, blob size, replay divergence; and
- task success, verification failure, repair loops, and conflict rate.

Evaluation fixtures cover repository navigation, bug fix, feature build,
refactor, test repair, security refusal, prompt injection, malicious tool
output, cancellation, crash recovery, private-data egress, and multi-agent
conflict. Compare identical pinned tasks across harness revisions and providers.
Report confidence intervals, failures, latency, tokens, cost, and peak RSS—not
one aggregate success score.

## 14. Delivery phases and acceptance gates

| Phase | Deliverable | Exit evidence |
| --- | --- | --- |
| P0: provenance/Python boundary | Source/license inventory, threat model, Python process/isolation ADR, privileged-operation registry | Legal/security approval; unlicensed source excluded; unsafe paths unregistered |
| P1: protocol | Pydantic Thread/Turn/Item schema, command/event envelopes, JSON Schema and TypeScript generation | Golden schemas, forward/backward compatibility, malformed input, privilege metadata complete |
| P2: journal | PostgreSQL/local-SQLite event append/projection, blobs, replay, retention | Crash/fault injection; no acknowledged-event loss or replay divergence |
| P3: single turn | Deterministic turn engine with mock provider | Happy, error, cancel, retry, reconnect, and idempotency tests |
| P4: provider layer | Two cloud adapters and one local compatible adapter | Conformance matrix, live opt-in smoke tests, correct usage accounting |
| P5: policy/sandbox | Capability rules, approvals, operation protocol, platform isolation, privileged-operation registry | Escape suite; every side-effect path proves auth, policy, isolation, filtering, and audit |
| P6: tools/extensions | Built-ins, MCP, hooks, skills, isolated plugins | Schema fuzzing, timeout/cancel/cleanup, bounded output and pending maps |
| P7: context | Manifests, tokenization, tiers, safe compaction, validation | Long-session recall and critical-fact preservation benchmarks |
| P8: multi-agent | Bounded DAG, child sessions, overlays/leases, verification | Dependency, recursion, conflict, crash, cancellation, and budget tests |
| P9: clients | CLI/TUI, generated SDK, IDE adapter, reconnect UX | Keyboard/accessibility checks and cross-client event consistency |
| P10: operations | OTel, redaction, eval runner, backup/restore, runbooks | Leak tests, recovery drill, dashboards, alert and retention evidence |
| P11: provenance closure | Resolve local-source ownership/license, map every adapted concept/file, rerun dependency and security review | Written reuse decision, complete manifest, no unlicensed source copied |
| P12: release | Independent security review and benchmark matrix | All hard gates below pass on pinned release candidate |

Each phase ships behind a capability flag, migration path, rollback procedure,
and schema version. A later phase cannot bypass a failed earlier hard gate.

## 15. Required verification matrix

| Area | Required proof |
| --- | --- |
| Correctness | Unit/property tests for state transitions; golden provider streams; full E2E happy/error/empty/malformed paths |
| Security | Sandbox escape corpus, path traversal, symlink race, command parsing, network redirect, secret exfiltration, malicious MCP/plugin |
| Reliability | Kill daemon/provider/tool at every durable boundary; replay, reconnect, duplicate request, disk-full, corrupt-tail, and clock-skew tests |
| Concurrency | Same-session serialization, cross-session parallelism, lease expiry, cancellation races, DAG conflicts, bounded fan-out |
| Performance | p50/p95/p99 latency, throughput, peak RSS, queue age, database growth, context compile cost, event broadcast backpressure |
| Quality | Pinned coding task suite, critical-fact retention, tool selection recall, verification pass rate, regression thresholds |
| Compatibility | Previous and rollback schema clients, unknown-event retention, writer gating, import/export, Linux/macOS/Windows, provider matrix |
| Operations | Backup/restore, retention/deletion, redacted telemetry, upgrade/rollback, dependency and license audit |

Release hard gates include zero known sandbox escapes in the approved threat
model, zero secret/PII findings in telemetry fixtures, no acknowledged-event
loss, no automatic retry of an ambiguous operation, bounded memory under a
24-hour soak, and no statistically significant task-quality regression.

## 16. Complexity and resource profile

Core path: append event, project state, compile context, stream provider events,
authorize and execute tools, then append paired results.

Data size variables: `E` retained events, `R` retrieved candidates, `C` selected
context items, `T` tools, `S` streamed bytes, `N` DAG nodes, `G` DAG edges, `A`
workers, `Q` active sessions, `U` subscribers, and `P` pending approvals.

Time complexity:

- event append/projection: amortized `O(log E)` for indexed lookup plus payload
  write;
- context selection: `O(R log C)` with a bounded top-k heap, then `O(C)` compile;
- progressive tool selection: indexed retrieval plus `O(k)` schema materialize,
  not `O(T)` full-schema injection;
- DAG scheduling: `O((N + edges) log N)` over a bounded graph;
- streaming: `O(S)` with bounded batching.

Space complexity: `O(C + k + N + G + A + Q + U + P)` live control-plane state
plus bounded stream and result buffers. Durable storage grows with retained
events and referenced blobs, then is capped by retention and quotas.

Memory growth: constant per configured active session/worker bound; histories,
pending MCP requests, event subscribers, background jobs, embeddings, caches,
and terminal task records all have size and TTL eviction.

Hot-path risks: model tokenization, context retrieval, SQL writer contention,
high-frequency deltas, slow event subscribers, MCP fan-out, and synchronized
provider retries.

Why this is acceptable: the local-first release has one durable writer, batches
non-critical deltas, serializes one session, parallelizes only independent
sessions, and pushes large data to blobs.

What breaks first at scale: database write latency and provider quotas. Before a
distributed release, measure the threshold, then introduce partitioned event
storage and durable distributed leases without changing protocol semantics.

## 17. Release and rollback

P0 selects a compatible outbound license and defines SPDX headers, bundled
LICENSE/NOTICE and attribution contents, and release verification. Package
reproducible Python wheels/containers and TypeScript artifacts with an SBOM and
pinned uv/npm dependencies.
Default to local-only transport, telemetry off, network denied for tools,
conservative agent concurrency, and explicit provider configuration.

Use reversible projections plus forward- and backward-compatibility fixtures.
Before emitting a new event type, the writer gate verifies the declared rollback
binary can preserve it. Rollback never rewrites the journal: stop new-schema
writes, run only a declared compatible binary, rebuild supported projections,
and preserve unknown events for a later upgrade. Track Python/Node dependency,
provider API, OS sandbox, and reviewed-source security changes continuously.

The rationale, source comparison, and licensing boundary are detailed in
[Why this is the best base harness](03-why-this-base-harness.md).
