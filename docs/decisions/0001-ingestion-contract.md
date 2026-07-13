# ADR 0001: PDF ingestion contract and provisional platform baseline

Status: approved for the P1 selected parser/OCR profile on 2026-07-13.

## Context

The service must ingest a corpus that can grow to one million PDFs without
blocking the API, retaining unbounded memory, or exposing one tenant's content
to another. A PDF is hostile, non-semantic input: it can be text-native,
scanned, mixed, malformed, encrypted, or resource-intensive. A successful
ingestion result must preserve source-page citations; an unsupported result
must be explicit and never silently searchable.

## Approved benchmark decisions

The product owner approved these defaults for P1 benchmark execution. They do
not select a production provider; a later reviewed decision replaces them only
when benchmark evidence justifies the change.

| Topic | Approved benchmark baseline |
|---|---|---|
| Tenancy | Logical multi-tenancy from day one. Every catalog, job, artifact, chunk, embedding, and search record has `tenant_id` and `collection_id`. |
| Authorization | OIDC/JWT identity integration; the catalog remains the authorization source of truth. |
| Upload source | Authenticated direct upload to approved object storage only. Arbitrary URLs are out of scope for v1. |
| Object storage | MinIO for the local S3-compatible benchmark; immutable generated keys, checksum validation, encryption settings, and lifecycle metadata are measured. |
| Catalog | PostgreSQL 18 for the P2 application runtime. The recorded P1 control benchmark remains PostgreSQL 16 with pgvector/GIN for reproducibility. No PDFs or large extracted blobs live in relational columns. |
| Queue | RabbitMQ with ID-only messages, publisher confirms, manual acknowledgement, DLQ, bounded prefetch, and queue-length/age limits. |
| Workers | Celery remains an execution candidate only; PostgreSQL owns idempotency and business state. Parser and OCR workers are isolated from the API and each other. |
| Native parsing | Select `pypdf==6.14.2` for born-digital text extraction with page provenance. It is not an OCR or layout-fidelity guarantee. |
| OCR/layout | Select Tesseract `5.3.4` with Apache-2.0 `eng` tessdata in a dedicated worker image. It is limited to printed English scans. Docling is not selected because it exceeds this host's repeatable memory capacity. Do not use PyMuPDF without explicit AGPL/commercial-license approval. |
| Search | Benchmark PostgreSQL plus pgvector/GIN as a control and OpenSearch hybrid retrieval as the scale candidate. Do not select either before filtered-retrieval tests. |
| OCR languages | Launch benchmark with `eng` only. Native Unicode extraction accepts any language but reports quality signals. |
| Retention | Content remains until explicit deletion or legal hold. Deletion revokes retrieval within five minutes, removes primary artifacts/indexes within 24 hours, and expires backups within 35 days. |

## Approved workload and locality baseline

The benchmark targets a future corpus of one million documents. Local runs use
only the synthetic fixture corpus and must not claim production scale. The
approved planning distribution is 70% small native PDFs, 20% complex/mixed or
scanned PDFs, 8% long/large PDFs, and 2% adversarial terminal cases.

The local comparative benchmark target is 100 documents/minute and 1,000
pages/minute. Default intake limits are 100 MiB and 500 pages per version. The
benchmark uses loopback-only Docker services and synthetic data; no customer
content, cloud account, or external data residency claim is permitted.

The benchmark must not assume one million in-memory items or one million
simultaneous jobs. It reports capacity parametrically:

```text
projected_chunks = documents × median_pages_per_document × median_chunks_per_page
required_stage_workers = peak_pages_per_minute ÷ measured_pages_per_worker_minute
```

All capacity reports must state the percentile distribution, worker shape,
provider quota, queue-age target, and headroom. A corpus-size claim without
those values is not a throughput target.

## Security and data contract

- Only files whose magic bytes begin with `%PDF-` and pass isolated parser
  inspection are eligible for processing. Names and client MIME types are hints.
- Original files and artifacts use generated storage keys; clients cannot choose
  object paths. Files are not served from the API/web root.
- Queue messages contain opaque IDs and bounded metadata, never PDF bytes,
  extracted text, signed URLs, secrets, or user-controlled paths.
- Parser/OCR containers run non-root, without outbound network by default, with
  read-only filesystems except a per-job temporary directory, and external CPU,
  memory, wall-time, output-size, and process limits.
- Content hashes may deduplicate only within the same tenant and compatible
  retention/ACL policy. Cross-tenant physical deduplication is prohibited.
- A document is searchable only when its complete, versioned chunk/index
  manifest is durable. Terminal outcomes publish zero chunks and zero vectors.

## Decision gates

P2 may implement only the selected native and OCR scopes after the fixture
corpus, reproducible benchmark evidence, and this ADR are approved. A candidate
may be rejected for licensing, egress, resource use, quality, tenancy-filtering,
or operational reasons even if its median speed is best.
