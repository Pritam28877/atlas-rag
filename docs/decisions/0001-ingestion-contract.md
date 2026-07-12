# ADR 0001: PDF ingestion contract and provisional platform baseline

Status: proposed — requires product-owner approval and benchmark evidence before P2.

## Context

The service must ingest a corpus that can grow to one million PDFs without
blocking the API, retaining unbounded memory, or exposing one tenant's content
to another. A PDF is hostile, non-semantic input: it can be text-native,
scanned, mixed, malformed, encrypted, or resource-intensive. A successful
ingestion result must preserve source-page citations; an unsupported result
must be explicit and never silently searchable.

## Provisional decisions

These defaults make the P1 benchmark concrete. They are not production
approval and must be replaced only through a reviewed decision update.

| Topic | Provisional decision | Confirmation required from |
|---|---|---|
| Tenancy | Logical multi-tenancy from day one. Every catalog, job, artifact, chunk, embedding, and search record has `tenant_id` and `collection_id`. | Product and security owner |
| Authorization | API uses an existing OIDC/JWT identity provider; the catalog remains the authorization source of truth. | Identity owner |
| Upload source | Authenticated direct upload to approved object storage only. Arbitrary URLs are out of scope for v1. | Security owner |
| Object storage | S3-compatible storage, immutable generated keys, checksum validation, encryption, lifecycle rules, and short-lived signed uploads. | Platform owner |
| Catalog | PostgreSQL 16+ for metadata, state, ownership, jobs, audit records, and index manifests. No PDFs or large extracted blobs live in relational columns. | Platform owner |
| Queue | RabbitMQ quorum queues with persistent ID-only messages, publisher confirms, manual acknowledgement, DLQ, bounded prefetch, and queue-length/age limits. | Platform owner |
| Workers | Celery is an execution adapter only; PostgreSQL owns idempotency and business state. Parser and OCR workers are isolated from the API and each other. | Platform owner |
| Native parsing | Benchmark `pypdf>=6.14,<7.0` first. It is not an OCR or layout-fidelity guarantee. | Engineering owner |
| OCR/layout | Benchmark `Docling==2.111.0` only in a dedicated worker image. Do not use PyMuPDF without explicit AGPL/commercial-license approval. | Engineering and legal owner |
| Search | Benchmark PostgreSQL plus pgvector/GIN as a control and OpenSearch hybrid retrieval as the scale candidate. Do not select either before filtered-retrieval tests. | Engineering and product owner |
| OCR languages | Launch benchmark with `eng` only. Native Unicode extraction accepts any language but reports quality signals. | Product owner |
| Retention | Content remains until explicit deletion or legal hold. Deletion revokes retrieval within five minutes, removes primary artifacts/indexes within 24 hours, and expires backups within 35 days. | Legal and product owner |

## Capacity planning assumption

The benchmark assumes a future corpus of one million documents. It must not
assume one million in-memory items or one million simultaneous jobs. Before P2,
the product owner must supply the actual page/byte distribution and peak
ingestion rate. Until then the benchmark reports capacity parametrically:

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

## Required stakeholder confirmations

P1 cannot be completed until these are confirmed or deliberately changed:

1. Cloud/on-prem deployment, region, data residency, and object-store owner.
2. Tenant/collection ACL model and OIDC/JWT authorization integration.
3. Expected document, page, byte, and peak-ingest distributions.
4. Maximum legitimate PDF size/page count, OCR languages, and table-fidelity need.
5. Retention, legal-hold, deletion, retrieval latency, relevance, and cost SLOs.
6. Git initialization or a documented waiver for the required parent branch flow.

## Decision gates

P2 is not authorized until the fixture corpus is approved, candidate benchmarks
are reproducible, all hard gates in the selection rubric pass, and this ADR is
approved. A candidate may be rejected for licensing, egress, resource use,
quality, tenancy-filtering, or operational reasons even if its median speed is
best.

