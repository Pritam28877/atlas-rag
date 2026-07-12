# ADR 0002: PDF support policy and safe terminal outcomes

Status: proposed — benchmark and product approval required before P2.

## Principle

Version 1 accepts PDF content subject to explicit limits and outcomes. It does
not promise universal semantic extraction, correct reading order, table
fidelity, handwriting recognition, or OCR accuracy. A terminal document must
not create chunks, embeddings, or index records.

## Support matrix

| PDF class | Route | Success outcome | Safe unsuccessful outcome |
|---|---|---|---|
| Born-digital text | Native parser | `READY` | `FAILED: PDF_TEXT_EXTRACTION_EMPTY` when no usable text and OCR is unavailable |
| Image-only scan | OCR after detection | `READY` | `FAILED: OCR_NO_TEXT_DETECTED`, `OCR_LANGUAGE_UNSUPPORTED`, or `OCR_QUALITY_BELOW_THRESHOLD` |
| Mixed text/image | Native + OCR on low-text pages | `READY` | Stage-specific native/OCR error with page provenance |
| Rotated/multilingual native | Native parser | `READY` or `READY_WITH_WARNINGS` | `READY_WITH_WARNINGS: TEXT_QUALITY_LOW` when citation coverage remains complete |
| Column/layout-heavy | Layout profile when enabled | `READY` or warning | `READY_WITH_WARNINGS: LAYOUT_READING_ORDER_UNCERTAIN` |
| Table-heavy | Table profile only when confidence passes | `READY` or warning | `READY_WITH_WARNINGS: TABLE_STRUCTURE_UNAVAILABLE` |
| Encrypted/password protected | Preflight | None | `REJECTED: PDF_ENCRYPTED_UNSUPPORTED` |
| Corrupt/malformed | Preflight/parser | None | `FAILED: PDF_MALFORMED` |
| File/page/output/resource breach | Intake or worker limit | None | `REJECTED: UPLOAD_SIZE_EXCEEDED`, `PDF_PAGE_LIMIT_EXCEEDED`, or `FAILED: PROCESSING_RESOURCE_LIMIT_EXCEEDED` |
| Suspicious/active content | Security inspection | None | `QUARANTINED: MALWARE_DETECTED`, `ACTIVE_CONTENT_DETECTED`, or `SECURITY_POLICY_VIOLATION` |
| Same content within compatible tenant scope | Intake deduplication | `DEDUPLICATED` | `DEDUPLICATED: DUPLICATE_CONTENT` with no duplicate work |

## State rules

Terminal states are `READY`, `READY_WITH_WARNINGS`, `DEDUPLICATED`,
`REJECTED`, `QUARANTINED`, and `FAILED`. `FAILED` is used only after a permanent
error or retry-budget exhaustion. Retryable errors retain their internal reason
and are retried with bounded exponential backoff plus jitter. `READY` and
`READY_WITH_WARNINGS` require at least one chunk and complete page provenance.

## Provisional resource policy

Hard maximums are enforced by signed-upload policy, database constraints,
broker settings, and worker/container limits—not by documentation or Python
checks alone. The values below are P1 benchmark guardrails, not final capacity
claims.

| Environment key | Default | Hard maximum | Owner / enforcement |
|---|---:|---:|---|
| `DOCUMENT_ALLOWED_MIME_TYPES` | `application/pdf` | PDF only | API and inspector |
| `DOCUMENT_UPLOAD_MAX_BYTES` | 100 MiB | 500 MiB | Upload policy and object HEAD |
| `DOCUMENT_PDF_MAX_PAGES` | 500 | 2,500 | Inspector |
| `DOCUMENT_PDF_ALLOW_EMBEDDED_FILES` | `false` | `false` | Inspector |
| `DOCUMENT_PDF_MAX_CONTENT_STREAM_BYTES_PER_PAGE` | 16 MiB | 32 MiB | Parser worker |
| `DOCUMENT_NORMALIZED_MAX_BYTES` | 256 MiB | 512 MiB | Artifact writer |
| `DOCUMENT_EXTRACTED_TEXT_MAX_CHARS` | 20,000,000 | 50,000,000 | Normalizer |
| `DOCUMENT_MAX_CHUNKS` | 10,000 | 25,000 | Chunker and database |
| `DOCUMENT_NATIVE_PARSE_TIMEOUT_SECONDS` | 300 | 900 | Worker supervisor |
| `DOCUMENT_OCR_TIMEOUT_SECONDS` | 900 | 1,800 | OCR supervisor |
| `DOCUMENT_NATIVE_PARSE_MEMORY_MIB` | 2,048 | 4,096 | Container/cgroup |
| `DOCUMENT_OCR_MEMORY_MIB` | 6,144 | 12,288 | Container/cgroup |
| `DOCUMENT_NATIVE_WORKER_CONCURRENCY` | 2 | 4 | Deployment |
| `DOCUMENT_OCR_WORKER_CONCURRENCY` | 1 | 2 | Deployment |
| `DOCUMENT_JOB_MAX_ATTEMPTS` | 3 | 5 | Orchestrator |
| `DOCUMENT_JOB_RETRY_BASE_SECONDS` | 30 | 60 | Orchestrator |
| `DOCUMENT_JOB_RETRY_MAX_SECONDS` | 900 | 3,600 | Orchestrator |
| `DOCUMENT_QUEUE_MAX_MESSAGE_BYTES` | 65,536 | 262,144 | Broker |
| `DOCUMENT_QUEUE_AGE_ALERT_SECONDS` | 900 | 3,600 | Monitoring |
| `DOCUMENT_OCR_LANGUAGE_CODES` | `eng` | Installed allowlist | OCR startup |
| `DOCUMENT_OCR_MIN_CONFIDENCE` | 0.70 | 0.90 | OCR normalizer |
| `DOCUMENT_DELETE_ACCESS_REVOKE_SECONDS` | 300 | 900 | Catalog/retrieval |
| `DOCUMENT_DELETE_PRIMARY_SLA_HOURS` | 24 | 72 | Deletion worker |
| `DOCUMENT_DELETE_EPHEMERAL_SLA_MINUTES` | 15 | 60 | Worker/cache purge |
| `DOCUMENT_BACKUP_RETENTION_DAYS` | 35 | 90 | Storage lifecycle |
| `DOCUMENT_RETENTION_DAYS` | 0 (explicit deletion) | Legal controlled | Lifecycle worker |

Legal hold revokes normal retrieval access but pauses physical deletion. OCR
confidence is engine- and language-specific; the 0.70 benchmark value requires
calibration on approved fixtures before use as a production quality gate.
