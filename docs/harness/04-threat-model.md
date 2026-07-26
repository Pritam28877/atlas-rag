# Atlas Harness threat model

Status: P0 baseline for review. Review date: 2026-07-26.

This model covers the release-one architecture in
[01-best-of-breed-architecture.md](01-best-of-breed-architecture.md). It is a
security contract, not evidence that controls are implemented. Each later PR
must link its control and adversarial verification back to the threat IDs here.

The current process boundary is defined by
[ADR 0003](../decisions/0003-python-harness-boundary.md). The
[privileged-operation registry](python-privileged-operation-registry.json) is
default deny and contains no registered operation in this feasibility phase.

## Security objectives

1. A command acts only for the server-authenticated principal and current
   workspace grant.
2. Every provider, telemetry, filesystem, process, network, secret, tool, MCP,
   plugin, parser, and child-agent side effect crosses an enforceable policy and
   budget boundary.
3. Approval narrows authority and never disables operating-system isolation.
4. Acknowledged state remains replayable; ambiguous external effects are never
   guessed or automatically repeated.
5. Credentials, prompts, file content, tool output, and personal identifiers do
   not enter logs, metrics labels, traces, crash reports, or unrelated providers.
6. Memory, queues, retries, events, artifacts, processes, agents, and storage
   remain within configured hard bounds.
7. Untrusted output cannot become instructions, authority, provenance, or an
   executable operation without validation.

## Protected assets

| ID | Asset | Required property |
| --- | --- | --- |
| A1 | Principal and grant | Authentic, current, revocable, server-derived |
| A2 | Workspace and repository | Tenant-isolated, canonical root, scoped writes |
| A3 | Thread, turn, event, and task state | Ordered, durable, replayable, tamper-evident |
| A4 | Operations and approvals | Exact request binding, fencing, durable outcome |
| A5 | Credentials and secret handles | Least privilege, destination-bound, never logged |
| A6 | Provider requests and responses | Policy-approved destination/data/cost, bounded |
| A7 | Tools, extensions, parsers, and child processes | Isolated, cancellable, resource-bounded |
| A8 | Context, memory, documents, and citations | Scoped, integrity-checked, provenance-bound |
| A9 | Telemetry and evaluation artifacts | Redacted, bounded, access-controlled |
| A10 | Release source and dependencies | Licensed, pinned, reproducible, attributable |

## Actors and trust assumptions

| Actor | Trust level | Assumptions |
| --- | --- | --- |
| Local user | Authenticated but fallible | May approve unsafe-looking work or open a hostile repository |
| API/IDE/automation client | Semi-trusted | May be stale, compromised, malformed, slow, or disconnected |
| Model provider and model output | Untrusted | May fail, drift, inject tool calls, leak data, or return malformed streams |
| Tool/MCP/plugin/hook/parser | Untrusted process | May be malicious, hung, memory-hungry, or attempt escape/exfiltration |
| Workspace content | Hostile input | Instructions, filenames, symlinks, archives, and source may be adversarial |
| Atlas control plane | Trusted coordination process | Must be authenticated, deterministic, bounded, fail closed, and never execute untrusted extension code in process |
| OS sandbox and egress proxy | Trusted enforcement | Capability detection must be accurate; unavailable controls cannot be assumed |
| SQLite/blob filesystem | Trusted for availability, verified for integrity | May become full, corrupt, reordered, or partially written |
| Operator/release maintainer | Privileged | Actions require audit, separation of duties, and documented recovery |

The model does not trust `client_id`, `workspace_id`, model text, tool output,
environment variables, file extensions, provider redirects, or plugin-declared
capabilities as proof of authority.

## Entry points and trust boundaries

| Boundary | Inputs | Mandatory controls |
| --- | --- | --- |
| Client to app server | Frames, commands, IDs, approval responses | Peer/process authentication, current grant, size/rate limits, schema validation, idempotency |
| App server to journal | Events, expected sequence, projections | Transactional append, CAS, durability class, payload hash, bounded metadata |
| Turn engine to provider | Context, tools, model policy, cost | Route explanation, data/cost policy, credential handle, egress allowlist, timeout/cancel |
| Turn engine to operation | Tool name, arguments, capability | Schema validation, args hash, allow/ask/deny, budget, fencing, durable prepare/dispatch |
| Sandbox to host | Files, processes, network, secrets | Namespaces/profile, explicit mounts, proxy, filtered environment, cgroups/limits |
| Extension/document host to control plane | Schemas, events, results, citations | Isolation, pending/output limits, validation, redaction, provenance checks |
| Journal to client/exporter | Durable events and telemetry | Reauthorization, sequence/resync, central redaction, bounded queue, egress policy |
| Parent to child agent | Task, artifacts, capability, budget | Intersection only, depth/fan-out caps, isolated workspace, typed handoff, verifier |
| Source to release artifact | Upstream code and dependencies | Immutable revision, license evidence, notices, SBOM, provenance manifest, review |

## Threats and required mitigations

### Identity, authorization, and confused deputy

| ID | Threat | Required mitigation and proof |
| --- | --- | --- |
| T01 | Client substitutes a workspace, tenant, thread, task, or artifact ID | Bind selectors to authenticated principal/current grant on every command, event subscription, approval, and range read; cross-scope E2E denial |
| T02 | Replayed/stolen local session token controls a persistent daemon | Owner-only socket/pipe, peer credentials, short expiry, rotation, revocation, channel binding, replay test |
| T03 | Approval is reused after args, actor, policy, tool, or version changes | Canonical request hash plus subject/scope/expiry/policy version; mutation matrix must fail |
| T04 | Layered policy accidentally broadens authority | Structured capability intersection, deny precedence, monotonic-restriction property tests |
| T05 | Child agent or extension grants itself permissions | Parent/current grant intersection at trusted boundary; adversarial role/manifest tests |

### Protocol, persistence, and recovery

| ID | Threat | Required mitigation and proof |
| --- | --- | --- |
| T06 | Malformed/oversized/fragmented frames exhaust or crash server | Bounded codec, schema validation, deadlines, concurrency admission, fuzz and RSS evidence |
| T07 | Duplicate/racing mutations create divergent state | Idempotency key, per-aggregate sequence, expected-version CAS, one-writer lease, race tests |
| T08 | Acknowledged event is lost or projection diverges | Atomic journal/projection transaction, synced durability classes, kill-point and replay-hash tests |
| T09 | Crash after external dispatch causes unsafe automatic retry | Durable Prepared/Dispatched receipts, idempotency/reconciliation class, `NeedsOperator` for ambiguity |
| T10 | Slow/disconnected subscriber grows memory or misses state silently | Bounded channel, delta coalescing, durable sequence ack, explicit resync marker, reconnect soak |
| T11 | Corrupt tail, disk-full, or rollback discards unknown facts | Integrity checks, disk reserve/read-only seal, fail-closed startup, unknown-event retention, recovery drill |

### Filesystem, process, and network execution

| ID | Threat | Required mitigation and proof |
| --- | --- | --- |
| T12 | Traversal, symlink race, hardlink, or mount escape accesses host data | Canonical workspace handles, descriptor-relative operations, explicit mounts, race/escape corpus |
| T13 | Shell/argument ambiguity executes an unapproved program | Executable plus parsed-argument capability; no shell-string-only decision; quoting corpus |
| T14 | Process escapes, forks indefinitely, survives cancellation, or consumes host | Namespace/profile, PID/cgroup/job limits, bounded output/time, owned process tree, kill/reap tests |
| T15 | Tool reaches arbitrary network, redirects to private host, or tunnels data | Default-deny proxy, DNS/IP/redirect revalidation, destination/method/port policy, SSRF suite |
| T16 | Required sandbox control is unavailable but work runs normally | Capability detection, fail closed or explicit read-only mode, missing-control tests |
| T17 | Direct privileged RPC bypasses policy/sandbox/audit | Registration denylist and CI reachability inventory; synthetic bypass must fail |

### Secrets, providers, and data egress

| ID | Threat | Required mitigation and proof |
| --- | --- | --- |
| T18 | Extension inherits daemon/cloud credentials | Opaque destination-bound handles, per-call injection, allowlisted environment, canary test |
| T19 | Secret or private content appears in logs/traces/errors | Central pre-sink redaction, safe structured errors, nested/malformed canary corpus |
| T20 | Router silently downgrades capability, region, privacy, or cost | Capability/data/region/budget filtering with recorded rejection reasons and versioned price |
| T21 | Retry/failover exceeds budget or repeats ambiguous work | Shared attempt/time/cost ledger, classified transient errors, no ambiguous automatic retry |
| T22 | Provider stream is malicious, malformed, infinite, or usage is false | Bounded decoder/channel/body/time, canonical validation, final usage reconciliation, malformed fixtures |
| T23 | Telemetry exporter becomes an ungoverned egress path | Same destination/data/redaction/quota/audit gate, telemetry off by default, outage/backpressure tests |

### Tools, extensions, context, and documents

| ID | Threat | Required mitigation and proof |
| --- | --- | --- |
| T24 | Tool/MCP/plugin/hook returns prompt injection or executable content | Typed untrusted result, schema/output validation, no authority, model-visible provenance labels |
| T25 | Hung MCP/plugin fills pending map or survives workspace close | Per-workspace host, pending/concurrency/time/output bounds, cancellation and process cleanup |
| T26 | Security hook fails open or notification hook mutates state | Trusted typed dispatcher; security gates fail closed; advisory behavior explicit; mutation denial tests |
| T27 | Compaction drops prohibitions, IDs, criteria, errors, or splits tool pairs | Safe boundaries, covered ranges/hashes, critical-fact validator, source restoration, long-context suite |
| T28 | Memory crosses tenant or overrides current authority | User/workspace namespace, provenance/expiry, bounded retrieval, context-only semantics, isolation tests |
| T29 | Malicious parser/upload causes RCE, SSRF, decompression bomb, or memory exhaustion | Sandbox, magic/MIME/size/page/time/output limits, streaming, no content-selected URL, parser corpus |
| T30 | Citation is model-authored, stale, cross-tenant, or does not match source | Immutable artifact/version/hash/span/retriever/tool binding and exact-source verification |
| T31 | Progressive tool discovery hides required tool or loads hostile schema | Bounded recall evaluation, trusted registry validation, explicit safe fallback, schema fuzzing |

### Multi-agent, operations, and supply chain

| ID | Threat | Required mitigation and proof |
| --- | --- | --- |
| T32 | Agent recursion/fan-out/cost/queue grows without bound | Hard depth/nodes/agents/provider/cost/time limits inherited across retries/resumes |
| T33 | Workers overwrite shared files or claim unchecked success | Worktree/overlay/file lease, staged conflicts, typed `not_checked`, independent verification |
| T34 | Stale worker applies result after lease loss | Fencing generation plus CAS checkpoint; stale result rejection and crash tests |
| T35 | Retention or GC deletes live/legal-hold evidence | Synced snapshot, reachability, tombstone, grace, separate audit retention, restore test |
| T36 | Dependency/update introduces malicious or unlicensed code | Locked dependencies, review policy, audit/deny, SBOM, provenance manifest, reproducible build |
| T37 | Unlicensed reference source is copied into Atlas | `copy_allowed=false`, adapted-file manifest gate, review, human license closure |
| T38 | Backup, release, or crash artifact leaks secrets | Encrypted/access-controlled artifact, central redaction, restore verification, canary scan |

## Resource-exhaustion invariants

The initial defaults and hard maxima in the implementation plan are security
controls. Admission counts queued and running work. Retry, reconnect, resume,
and child sessions consume the original allocation. Every bounded resource must
define:

- owner and accounting scope;
- default and hard maximum;
- timeout/cancellation behavior;
- overflow result visible to clients and telemetry;
- cleanup/retention behavior; and
- a soak assertion proving memory and storage converge.

No global map, cache, queue, task, subscriber, process, blob, event segment,
memory namespace, or terminal job may grow without size and lifetime limits.

## Residual risks and release gates

- Linux is the first executable security target. macOS and Windows production
  claims remain blocked until equivalent native escape evidence exists.
- Provider behavior, pricing, retention, and regional guarantees can change.
  Catalog revisions and live conformance evidence expire by release policy.
- OS sandboxing reduces impact but cannot prove a kernel or hypervisor is safe.
  Security updates and independent review remain required.
- The local Orqen and documentIntelligence trees have no visible license grant.
  Their concepts may inform requirements; their files remain forbidden input to
  production adaptation.
- Human users can approve harmful commands. Atlas must show the exact scope,
  arguments, destination, data class, budget, and expiry and avoid broad
  approval suggestions.

Release remains blocked until every threat has implemented control evidence,
the approved escape/exfiltration suites have zero known bypass, telemetry canary
scans have zero findings, acknowledged-event recovery is lossless, ambiguous
effects stop safely, and the 24-hour soak proves all resource growth is bounded.
