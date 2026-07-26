# Codex integration spike and boundary decision

Status: accepted implementation decision for the pinned P0 snapshot.

Reviewed source:
[`openai/codex@4c434651`](https://github.com/openai/codex/tree/4c43465133428898aa84f0bfc02c306ed65fb66a).
Review date: 2026-07-26. Toolchain declared by that snapshot: Rust 1.95.0.

## Decision

Atlas will not embed, proxy, or register the pinned `codex-app-server` as its
control plane. Atlas will own authentication, grants, protocol registration,
journal/projections, provider routing, policy, budgets, operation durability,
egress, redaction, and extension isolation.

Codex remains the selected security and agent-kernel source. Atlas may adapt
specific Apache-2.0 files from its protocol/core/sandbox/exec-policy crates only
after a file-level provenance record and boundary review. Thread/Turn/Item,
resume/fork/compact, approval, tool, and event behavior is reimplemented behind
Atlas gates rather than forwarded to raw upstream RPC handlers.

Unlisted upstream RPCs default to denied. The machine-readable disposition is
[`codex-privileged-rpc-inventory.json`](codex-privileged-rpc-inventory.json).

## Evidence

The sparse checkout was pinned by full commit and measured from Rust source:

| Area | Measured size |
| --- | ---: |
| `codex-rs/app-server/src` | 44,281 lines |
| `codex-rs/app-server` direct dependencies | 72 |
| `codex-rs/app-server-protocol/src` | 29,400 lines |
| `codex-rs/execpolicy/src` | 1,974 lines |
| `codex-rs/linux-sandbox/src` | 6,860 lines |

The app server is therefore not a narrow reusable dependency. Direct reuse
would pull authentication/account, configuration, marketplace/plugin, remote
control, environment, realtime, provider, MCP, filesystem, process, feedback,
and migration surfaces into the trusted boundary before Atlas enforcement
exists.

The source confirms concrete bypasses:

- `thread/shellCommand` calls itself the local-host escape hatch. The core path
  uses `PermissionProfile::Disabled`, `SandboxType::None`, and no managed proxy.
- `process/spawn` is documented as standalone unsandboxed execution and starts
  from all daemon environment variables before client overrides.
- `fs/readFile`, `fs/writeFile`, directory, metadata, remove, and copy accept
  absolute paths and call the executor filesystem with `sandbox: None`.
- `command/exec` accepts client-provided permission/sandbox parameters and
  flags that remove timeout and output caps.
- extension installation/sharing, MCP OAuth/tools, feedback, account mutation,
  remote control, and environment creation combine credential, network,
  filesystem, or external side effects.

Serialization scopes in the upstream protocol coordinate requests; they are
not principal authorization, tenant isolation, policy decisions, operation
receipts, or OS containment.

## Approved reuse boundary

| Upstream area | P0 disposition | Entry condition |
| --- | --- | --- |
| `codex-app-server` handlers/binary | Do not reuse | None for release one |
| Raw `ClientRequest` registration | Do not reuse | Compatibility alias only after Atlas handler/gate proof |
| Thread/Turn/Item/event semantics | Reimplement | Atlas schema, journal, idempotency, auth, pagination, compatibility tests |
| `codex-app-server-protocol` source | File-by-file only | Provenance entry and removal of privilege-carrying variants |
| `codex-core` state/tool algorithms | File-by-file only | No provider credential, host execution, global state, or upstream handler dependency |
| `codex-execpolicy` | Candidate adaptation | Structured Atlas capability wrapper, deny precedence, property tests |
| `codex-linux-sandbox`/sandboxing | Candidate adaptation | Capability detection, fail-closed policy, Atlas operation supervisor and escape suite |
| Small utilities | Candidate adaptation | Narrow dependency graph, provenance, license notice, independent tests |

No Codex source is copied in this PR. P3 starts with an Atlas-owned workspace;
later source adaptation must add `adapted_files` metadata before review.

## Fork and update budget

The maintained surface is constrained as follows:

1. no dependency on the upstream `codex-app-server` binary or handler crate;
2. no raw upstream method registration;
3. each adapted file has one Atlas owner and one upstream path/revision;
4. security-sensitive adaptation remains in a named trusted-kernel crate;
5. a monthly upstream scan checks license, privileged RPCs, sandbox, exec policy,
   protocol compatibility, and security advisories;
6. security updates are triaged within two working days;
7. a routine upstream refresh should fit within two engineer-days; and
8. exceeding that budget for two consecutive refreshes triggers an ADR to
   reduce, replace, or rebase the adaptation surface.

These values are an initial governance budget. P3/P4 records actual adapted-file
count and compilation cost; the release gate replaces estimates with measured
maintenance evidence.

## Registration gate

`scripts/verify_codex_rpc_inventory.py` enforces:

- full immutable upstream revision and exact toolchain;
- default-deny disposition;
- unique groups and method ownership;
- required evidence and Atlas replacement for every group;
- a fixed critical deny set for raw shell, process, command, and filesystem
  methods; and
- optional rejection of a proposed registered-method list containing a denied
  or unclassified upstream method.

This is defense in depth. P19 later maps every compiled Atlas route to
authentication, workspace authorization, schema/size, idempotency/sequence,
policy/budget, sandbox/egress, result filtering/redaction, and durable audit.

## Alternatives rejected

### Proxy an unmodified Codex app server

Rejected. A filtering proxy would make method names the security boundary while
the child process still owns provider, credentials, host execution, state, and
extensions. It also cannot provide atomic Atlas event/projection semantics.

### Depend directly on the full app-server crate

Rejected. Its measured source/dependency surface and privilege-bearing handlers
would expand the trusted kernel before the required gates exist.

### Copy all reviewed harnesses together

Rejected. It would introduce multiple state machines, incompatible permission
and retry semantics, unlicensed local source, and an unreviewable patch surface.

### Ignore Codex and build an unrelated Python agent loop

Rejected. The harness specification deliberately selects Rust for the trusted
kernel and Codex as the strongest licensable security/agent source. The existing
FastAPI RAG service remains a later typed document adapter, not the harness
executor.

## P3 entry contract

P3 may begin only when this inventory verifier passes. It creates the Rust
1.95.0 workspace, locked dependency graph, CI/audit/license gates, and empty
narrow crates. It registers no provider, tool, filesystem, process, plugin, MCP,
or remote-control operation.
