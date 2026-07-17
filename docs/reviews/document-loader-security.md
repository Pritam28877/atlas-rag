# Document loader security and maintainability review

Review date: 2026-07-17. Scope: intake, parsing/OCR, publication, reprocessing,
deletion, reconciliation, and operational controls.

| Area | Evidence and disposition |
|---|---|
| Authorization / IDOR | Every catalog query and search mutation binds tenant, collection, and version identifiers. Cross-scope API resources return not found; real OpenSearch tenant denial is tested. |
| Upload validation | Direct upload has size, MIME, PDF magic, checksum, metadata, quota, and idempotency checks. Unsupported/encrypted/corrupt/active-content fixtures have explicit policies. |
| Parser isolation | Non-root read-only worker images, bounded tmpfs, CPU/RSS/pid/time/page/output limits, no embedded-file execution, and separate OCR capacity. |
| SSRF / egress | Clients use configured endpoints and server-generated storage keys. Document content cannot select a URL, bucket, index, or model. |
| Encryption / secrets | TLS verification defaults on; production validation rejects insecure search. Credentials use secret settings and are absent from structured logs and metric labels. Rotate any credential ever exposed outside the secret store. |
| Provenance / integrity | Immutable version/artifact identity, exact checksums, deterministic chunk IDs, pinned model revision/checksum, and database-enforced ready evidence. Reprocess creates linked immutable versions. |
| Deletion / privacy | Search revocation is tenant/version scoped and precedes cleanup. Active work is cancelled. Retention and legal hold control physical deletion; lifecycle requests remain as content-free audit records. |
| Resource bounds | Upload bytes, pages, normalized output, text, blocks, chunks, embedding batch/concurrency, workers, retries, queue, deletion batch, and reconciliation page are all capped. |
| Concurrency | Row locks, leases, unique identities, late acknowledgements, bounded retry, and idempotent provider operations handle duplicate/reordered work. |
| Observability | Database-backed queue metrics use fixed-cardinality labels; IDs are excluded from metrics. Shipped alerts cover queue age and dead-letter growth. Worker counters and trace propagation require a deployment exporter before operational use. |
| Cleanup | Database/storage/search clients close in `finally`; temporary job directories are scoped; object deletion validates the managed prefix. |
| Maintainability | Feature modules are typed, tested, use provider protocols, and avoid generated or duplicate implementations. Files are kept below the 500-line hard cap and split at the 400-line threshold. |

`uv run --with pip-audit pip-audit` reported no known vulnerabilities after
upgrading FastEmbed to 0.8.0, Pillow to 12.3.0, and pytest to the fixed 9.x line.

No known critical authorization, privacy, unbounded-memory, or data-integrity issue
remains in this scope. Remaining deployment risks are target-environment capacity,
backup expiry enforcement outside primary storage, and the currently reported
Starlette TestClient deprecation warning; these do not weaken runtime isolation
but must be tracked by operations/dependency maintenance.
