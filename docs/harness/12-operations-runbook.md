# Atlas Harness operations runbook

This runbook defines bounded, disposable recovery drills. It is an operator
checklist, not evidence that a live cloud provider or 24-hour soak has run.

## Safety and ownership

- **Incident owner:** Harness on-call; **backup owner:** Persistence owner;
  **security owner:** Security on-call; **release approver:** Platform lead.
- Use a private temporary directory created with `mktemp -d` and record its
  absolute path. Never point a drill at a production database or artifact root.
- Keep the command transcript, UTC start/end times, exit status, manifest hash,
  and restore verification result under the incident evidence record.
- Default recovery objectives are RPO 5 minutes and RTO 30 minutes for a local
  workspace. A provider outage is failover-only and must not create provider
  credentials or cloud spend.

## Baseline evidence

Run from the repository root:

```bash
evidence_dir="$(mktemp -d)"
umask 077
uv run --locked pytest -q tests/harness/recovery | tee "$evidence_dir/recovery.log"
uv run --locked ruff check app/services/harness/recovery tests/harness/recovery \
  | tee "$evidence_dir/ruff.log"
uv run --locked mypy app/services/harness/recovery \
  | tee "$evidence_dir/mypy.log"
```

Record `evidence_dir`, the three exit statuses, the current UTC timestamp, and
the commit identifier. Delete the disposable directory only after the evidence
has been retained by the incident system.

## Drill matrix

| Drill | Owner | Command or setup | Safe outcome | Target | Evidence |
| --- | --- | --- | --- | --- | --- |
| Disk full | Persistence | Fill only the disposable filesystem until the configured reserve is reached | Writes stop or seal read-only; no acknowledged journal record is lost | 15 min | reserve threshold, error code, journal hash |
| Corrupt tail | Persistence | Copy the disposable SQLite file, alter only its final journal bytes, then run startup recovery | Startup refuses the corrupt tail and preserves the last verified prefix | 15 min | corrupt offset, recovery status, prefix hash |
| Clock skew | Harness on-call | Run with an injected UTC clock outside the allowed lease window | Lease and timestamp validation fail closed; no future-dated event is accepted | 10 min | skew amount, rejected operation, UTC clock |
| Expired lease | Harness on-call | Advance the injected clock past a disposable worker lease | The stale worker is fenced, evidence is written, and a new owner may reclaim the node | 10 min | lease generation, owner IDs, recovery evidence hash |
| Provider outage | Harness on-call | Disable the configured provider transport in the local conformance fixture | The request ends with a bounded provider failure or approved failover; no unbounded retry loop occurs | 10 min | provider code, retry count, failover decision |
| Tool hang | Security | Use the bounded tool fixture that never completes | Deadline/cancellation terminates the tool scope and releases resources | 10 min | operation ID, deadline, scope cleanup |
| Export outage | Observability | Point the disabled-by-default exporter at a refusing local endpoint | Redacted events are bounded, export retries are capped, and drops are counted | 10 min | queue depth, retries, drops, redaction result |
| Retention/legal hold | Persistence + security | Place a disposable legal hold, attempt collection, release the hold, then collect | Collection is denied while held and becomes eligible only after an explicit release | 15 min | hold digest, release time, collection decision |

## Backup and restore procedure

1. Quiesce writes for the disposable workspace and capture the database,
   artifact, configuration, and provenance component digests in a sorted
   `BackupManifest`.
2. Copy the database through the storage adapter's transaction boundary; do
   not copy a live WAL file by hand. Store the manifest beside the backup using
   its content address.
3. Restore into a new private disposable directory. Verify every component,
   including size and SHA-256, before opening the restored service.
4. Rebuild projections from the restored journal and compare projection and
   artifact hashes with the manifest. Missing, corrupt, duplicate, or unexpected
   components are a failed restore, never a warning.
5. Record the manifest digest, restore verification, projection hashes, and
   elapsed time. Keep the source backup unchanged.

## Upgrade and rollback procedure

1. Generate an `UpgradePlan` with the target writer schema and every declared
   rollback reader. The writer gate must be allowed before writes start.
2. Preserve unknown command/event bytes exactly when an older reader cannot
   project them. Never rewrite or delete the append-only journal during an
   upgrade or rollback.
3. Rebuild only compatible projections in the disposable restore and compare
   hashes. If any reader is outside the compatibility window, block the writer
   and retain the rejection reason.
4. Record the gate decision, preserved-record count, projection hashes, and
   rollback-reader result as release evidence.

## Escalation and abort rules

Abort a drill immediately if its path is not disposable, a secret appears in
output, the evidence directory is not private, or a command would contact a
cloud provider. Escalate any hash mismatch, accepted write after a failed gate,
unbounded retry/queue growth, or cleanup failure to the platform lead; do not
retry destructively.
