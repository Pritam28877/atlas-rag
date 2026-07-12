# Native parser benchmark: pypdf 6.14.2

Run date: 2026-07-12

## Scope

This is a reproducible local, synthetic-corpus benchmark. Its generated record
is intentionally ignored at `benchmarks/results/native-parser-pypdf.json` and
must be reproduced with the command below. It evaluates only the native parser
candidate; it does not select an OCR engine, object store, broker, database,
embedding provider, or search engine.

## Environment and input

- Python 3.12.3, `pypdf==6.14.2`, ReportLab-generated fixtures.
- Twelve hash-verified, permission-safe PDFs under `benchmarks/fixtures/`.
- One small 1,024-byte threshold exercises the limit-rejection fixture. It is
  not a proposed production upload limit.
- The corpus includes native text, multi-column, table, scan, mixed, rotated,
  multilingual, encrypted, corrupt, limit, suspicious-marker, and duplicate
  cases. Hashes, page counts, routes, and expected outcomes are in the fixture
  manifest.

## Expected local result

The manifest-driven runner validates all 12 fixture outcomes on a supported
development host. It fails on an unmanifested fixture, hash mismatch, missing
golden, mismatched page count, unexpected route/state/reason code, or a failed
page-aware golden assertion.

| Category | Outcome |
|---|---|
| Native, multi-column, table, rotated, multilingual | Text extracted with full page citation coverage on this synthetic corpus. |
| Scan | Native extraction produced no text; correctly identified as OCR-routed. |
| Mixed | Two pages retained per-page distinction; native text coverage was 0.5, requiring OCR for the image page. |
| Encrypted | Rejected as `PDF_ENCRYPTED_UNSUPPORTED`. |
| Corrupt | Rejected as `PDF_MALFORMED`. |
| Limit profile | Rejected as `UPLOAD_SIZE_EXCEEDED`. |
| Suspicious PDF action | Quarantined as `ACTIVE_CONTENT_DETECTED` after detecting a real catalog `/OpenAction` JavaScript dictionary. |
| Duplicate | Identified as `DUPLICATE_CONTENT`; this validates the intended routing only, not a database uniqueness constraint. |

## Decision

`pypdf==6.14.2` remains the provisional native-text parser candidate for P2
implementation. It is not selected for OCR or layout/table semantics. The
candidate has not passed the production gate until it is measured against the
approved representative corpus under the configured memory, wall-time, and
content-stream limits.

## Limitations and next evidence

- The suspicious fixture contains an inert JavaScript `/OpenAction`; it verifies
  PDF object-graph detection only, not malware scanning or complete policy coverage.
- The corpus is intentionally small and synthetic; it cannot demonstrate
  customer-layout quality, real workload throughput, or million-document scale.
- This host has no OCR engine installed; Docling/OCR quality, model size, cost,
  languages, and isolation remain unbenchmarked.
- Storage, RabbitMQ, PostgreSQL, embedding, hybrid retrieval, tenancy filters,
  deletion, retry, and failure recovery remain unbenchmarked because no approved
  deployment or service configuration exists.
