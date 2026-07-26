# ADR 0003: Python-first Atlas Harness boundary

Status: approved implementation direction on 2026-07-26; production release
still requires every gate in the harness plan.

## Context

Atlas needs durable agent sessions, multiple model providers, controlled tools,
MCP and plugin processes, document retrieval, multi-agent scheduling, and
reconnectable clients. The repository already operates a Python 3.12 FastAPI
service with Pydantic settings, authentication, SQLAlchemy/Alembic persistence,
PostgreSQL integration tests, S3-compatible storage, Celery workers,
OpenTelemetry, and uv dependency locking.

An earlier unmerged proposal added a Rust control plane. The product owner
changed the current implementation direction to Python and requested a separate
Rust plan for later. Rust PRs #16 and #17 were closed. No Rust source, toolchain,
build step, sidecar, or extension module is part of this decision.

Python is not an OS sandbox. The decision is viable only if arbitrary tool,
model-authored, MCP, plugin, hook, parser, and user code never executes in the
FastAPI process.

## Decision

Implement Atlas as feature-oriented Python modules under
`app/services/harness`. Pydantic v2 models own the canonical domain and wire
schema. FastAPI, SQLAlchemy, provider clients, storage, subprocess execution,
and client transports are adapters around domain protocols.

The trusted Python process may perform:

- strict request and event validation;
- authentication and server-derived grant binding;
- deterministic state transitions and projection reducers;
- policy, budget, route, context, and scheduling decisions;
- durable journal and artifact coordination through bounded adapters;
- redaction and bounded telemetry coordination; and
- ownership of queues, tasks, connections, provider requests, and child
  processes.

The trusted Python process must not execute:

- shell text or model-generated Python/JavaScript;
- tool, MCP server, plugin, or third-party hook implementation code;
- document parser/OCR code handling hostile bytes;
- unreviewed provider callbacks;
- dynamically imported workspace code; or
- any operation absent from the privileged-operation registry.

Those workloads run in owned subprocesses or isolated workers with a compiled
capability profile. Required controls include environment allowlisting,
canonical workspace mounts, default-denied network, resource and output limits,
deadline, cancellation, descendant cleanup, result validation, redaction, and
audit. Missing required isolation fails closed.

## Package and dependency direction

```text
protocol + domain + events
        <- runtime + policy + context + scheduler
        <- journal/artifact/provider/tool/sandbox adapters
        <- FastAPI + worker + CLI composition roots
```

Domain modules cannot import FastAPI, Celery, SQLAlchemy, boto3, HTTP clients,
provider SDKs, storage clients, subprocess helpers, CLI/TUI packages, or
generated clients. Infrastructure adapters may depend on domain protocols, but
domain code cannot depend on an adapter.

Only the sandbox supervisor may create a tool or extension subprocess. Only the
provider egress gateway may open model-provider connections or resolve provider
credentials. Only the telemetry egress gateway may export traces. Application
composition roots register these adapters explicitly.

## Async and resource ownership

Every long-lived resource has one owner:

| Resource | Required ownership |
| --- | --- |
| Async task | Structured task group or named lifecycle owner; exceptions observed |
| Queue/channel | Fixed capacity, overflow policy, producer/consumer shutdown |
| Semaphore/pool | Configured default and hard maximum |
| Provider request | Deadline, cancellation signal, retry/cost budget, response cap |
| Database operation | Bounded pool, transaction scope, statement timeout, cleanup |
| Subscriber | Fixed queue, durable sequence, resync marker, disconnect cleanup |
| Subprocess | New process group, bounded pipes, deadline, terminate/kill escalation, wait |
| Artifact stream | Byte limit, digest, cancellation, context-managed close |
| Cache/history | Maximum entries and bytes, TTL/invalidation, eviction |

Blocking SDK and CPU-heavy work runs through bounded executors or isolated
workers. No bare `asyncio.create_task`, ownerless future, unbounded
`asyncio.Queue`, unbounded gather, synchronous network call, or blocking
subprocess wait is permitted on a request/event-loop path.

## Privileged-operation admission

The machine-readable registry at
`docs/harness/python-privileged-operation-registry.json` is default deny. Each
planned or registered operation declares its owner, boundary kind, side-effect
class, required enforcement gates, failure mode, and registration state.

A registered implementation must be repository-relative and must pass the
minimum gate set for its kind. Synthetic omissions fail the registry verifier.
The initial registry contains planned-disabled operations only; this ADR does
not register a harness route, worker task, tool, provider connection, telemetry
exporter, parser, plugin, MCP server, secret resolver, or subprocess path.

## Linux feasibility decision

Linux is the first executable isolation target. The P2 probe requires:

- bubblewrap and unprivileged user namespaces;
- a new user, PID, IPC, UTS, cgroup, and network namespace;
- read-only runtime libraries, private `/tmp`, minimal `/dev` and `/proc`;
- only the selected workspace mounted writable;
- a cleared environment with explicit values only;
- a new host process group owned by the supervisor;
- bounded stdout/stderr and wall time;
- terminate/kill escalation and mandatory reap; and
- fail-closed behavior when bubblewrap is absent.

Cgroup CPU/memory/PID enforcement and proxy-mediated allowlisted network are
required before general tool execution, but are not claimed by the P2
feasibility probe.

macOS Seatbelt and Windows restricted-token/Job Object support remain
non-production until equivalent native escape and cleanup evidence exists.

## Rollback

Harness settings default disabled. Before route registration, enabling the
feature requires validated settings, migration readiness, and isolation
capabilities appropriate to enabled operations. Rollback stops admission,
drains/cancels owned work, disables harness routes/workers, and preserves the
append-only journal and artifacts for a compatible reader.

No harness change may alter the existing ingestion/catalog runtime when the
feature is disabled.

## Deferred Rust interface

The domain schema, event journal, provider contracts, privileged-operation
registry, process supervisor contract, and generated client protocol are
language-neutral boundaries. A later Rust candidate must implement those
contracts out of process, shadow without side effects, prove Python/Rust parity,
and preserve Python rollback.

Rust activation requires the separate R1 decision with measured Python
latency, throughput, peak RSS, task/process/descriptor counts, cancellation
latency, security results, scope, budget, owner, thresholds, and abort criteria.

## Consequences

Positive:

- one current runtime, dependency manager, deployment stack, and operations
  model;
- fastest integration with existing authentication, storage, database, workers,
  telemetry, and test environments;
- clear seams for provider replacement and later selective Rust migration; and
- no trusted in-process third-party extension surface.

Costs and risks:

- Python scheduling, garbage collection, and blocking dependencies require
  measured tail-latency and memory gates;
- OS containment must be engineered explicitly and cannot be inferred from
  Python;
- blocking boto3 or parser work needs bounded worker isolation;
- process boundaries add serialization and supervision overhead; and
- the API process remains a high-value target requiring small modules, strict
  validation, and independent security review.

## Reconsideration triggers

Open the deferred Rust R1 decision only when at least one measured condition
persists after bounded Python optimization:

- request/stream p99 misses the approved SLO;
- realistic 24-hour RSS growth exceeds the configured bound;
- subprocess/cancellation correctness cannot meet the escape suite;
- required concurrency cannot be reached within CPU/memory budgets;
- Python packaging or cross-platform supervision blocks release; or
- an independent security review requires a smaller native control boundary.

Passing a microbenchmark alone is insufficient.
