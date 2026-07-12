# PDF ingestion benchmark protocol

Status: required before P1 selection. The manifest in this repository describes
the corpus; fixture binaries, golden transcripts, raw logs, and benchmark
results are access-controlled and must not be committed unless synthetic or
publicly redistributable.

## Reproducibility record

For every run, record corpus version and hashes, harness commit, `uv.lock` hash,
container image digest, exact package/service/model versions, model checksums
and download bytes, region, CPU/GPU/RAM/disk, network mode, cache mode,
concurrency, seed, and trial number. Run each parser/OCR profile at least five
times in cold and warm modes; report median, p95, max, and confidence interval.

## Execution sequence

1. Approve the P1 contract: tenant model, residency, languages, limits, OCR and
   table policy, retention, retrieval SLA, cost envelope, and workload shape.
2. Acquire/generate fixtures through `fixture-manifest.json`; verify permission,
   classification, SHA-256, page count, no secrets/PII, and golden references.
3. Build network-restricted candidate images. Do not let parser/OCR candidates
   fetch external resources during a measured run.
4. Run native parser/OCR candidates one fixture at a time under the resource
   policy. Persist normalized artifacts and compare page-aware goldens.
5. Run bounded-concurrency throughput tests at 1, configured worker count, and
   overload. Measure backpressure rather than accepting unbounded queues.
6. Generate deterministic chunks using one fixed profile. Re-run and compare
   normalized/chunk hashes; document approved nondeterminism if any.
7. Index the same chunk and embedding snapshot in each retrieval candidate;
   measure lexical-only, vector-only, and hybrid results against independent
   human relevance labels under broad and restrictive tenant filters.
8. Test object storage, broker, catalog, embedding, and search independently,
   then run upload-complete-to-ready end-to-end tests.
9. Run every failure-injection case. A terminal failure passes only when it
   creates zero chunks/vectors and matches the documented reason code.
10. Write append-only result JSON and a decision report containing winners,
    rejected candidates, limitations, raw-log references, and next actions.

## Required metrics

| Area | Metrics |
|---|---|
| Parser/OCR | Text fidelity, reading order, table score where required, page-citation coverage, false-ready-empty rate, route accuracy, p50/p95 duration, pages/minute, RSS, CPU/GPU seconds, output bytes, timeout/OOM rate, determinism, model bytes, license, cost/page |
| Chunking | Chunks/document, token distribution, over-limit count, provenance coverage, duplicate count, deterministic hash rate |
| Storage | Upload/download p50/p95/p99, throughput, multipart-resume behavior, checksum failures, read-after-write behavior, cost |
| Broker | Publish/consume latency, message rate, queue age/depth, redelivery, restart durability, DLQ behavior, recovery |
| Catalog | Transaction/query p95, conflict/deadlock rate, index/storage growth, connection saturation, query plans |
| Embeddings | Tokens/sec, batch latency, provider throttling/errors, cost, model reproducibility, quota behavior |
| Search | Recall@5/@10, nDCG@10, MRR@10, citation hit accuracy, p50/p95/p99, index build/update/delete time, filter latency, cross-tenant leakage |
| End to end | Upload-complete-to-ready p50/p95, document/page throughput, queue age, terminal-state correctness, cost/document, deletion cleanup |

## Required failure injection

| ID | Scenario | Required result |
|---|---|---|
| FI-01 | Renamed non-PDF | Rejected before parser; no derivatives/index |
| FI-02 | Truncated/corrupt PDF | Explicit failure; no retry loop |
| FI-03 | Encrypted PDF | Explicit rejection; no empty ready result |
| FI-04 | Byte/page/output limit breach | Configured limit reason and bounded cleanup |
| FI-05 | High-memory/decompression input | Worker containment; API and peer jobs stay healthy |
| FI-06 | Parser/OCR hang | Timeout, lease recovery, bounded retry/DLQ |
| FI-07 | OCR model unavailable | Classified safe failure; no unverified fallback |
| FI-08 | Worker kill after artifact write | Idempotent redelivery; one logical result |
| FI-09 | Worker kill during indexing | Never partly searchable; reconciliation signal |
| FI-10 | Duplicate/reordered message | Same final state; unique records |
| FI-11 | Broker full/unavailable/restarted | Bounded producer and durable recovery |
| FI-12 | Storage timeout/checksum mismatch | No corrupt artifact accepted |
| FI-13 | Database failover/deadlock/race | Transaction-safe recovery; no duplicate version |
| FI-14 | Embedding throttle/timeout | Bounded retry/jitter; no queue storm |
| FI-15 | Search partial write/outage | Not ready until complete; repair signal |
| FI-16 | Delete during any stage | Cancellation and cleanup; no post-delete retrieval |
| FI-17 | Tenant-filter bypass | Zero unauthorized result; security event |
| FI-18 | Actions/attachments/external reference | No execution/egress; explicit policy result |
