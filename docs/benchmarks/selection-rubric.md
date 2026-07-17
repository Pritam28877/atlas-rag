# Component selection rubric

Only candidates that pass every hard gate may be ranked. A fast component that
fails tenancy, provenance, resource, license, or recovery requirements is
rejected.

## Hard gates

1. Security: zero cross-tenant results, no unapproved egress, no executable PDF
   content, and no secrets/PII in logs or reports.
2. Correctness: all malformed, encrypted, limit, and suspicious fixtures reach
   explicit outcomes; terminal outcomes create no chunks/vectors; indexed chunks
   have complete page provenance.
3. Resilience: duplicate delivery/retry cannot duplicate logical versions,
   artifacts, chunks, embeddings, or index entries; acknowledged completion
   survives worker/storage/broker restart.
4. Resources: CPU, RAM, wall time, output size, concurrency, and retry ceilings
   remain within the approved contract. No uncontrolled model download.
5. Operations: provider supports approved deployment/residency, acceptable
   license, observable failures, bounded backlog, and documented recovery.
6. Retrieval: tenant/collection filters are correct and relevance/latency meet
   the approved workload target for broad and restrictive filter cases.

## Weighted ranking after gates

| Dimension | Weight | Evidence |
|---|---:|---|
| PDF/OCR quality and citation fidelity | 30% | Class-specific fixture scores, not corpus average only |
| Security, reliability, and operability | 25% | Failure matrix, recovery, observability, isolation |
| Retrieval quality and filtered latency | 20% | qrels, Recall@10, nDCG@10, p95, leakage test |
| Cost and capacity efficiency | 15% | Cost/page, storage/index footprint, worker throughput |
| Maintainability and licensing fit | 10% | Upgrade path, model/dependency controls, legal approval |

## Candidate matrix

| Component | Candidate | Status | Required evidence |
|---|---|---|---|
| Native parser | pypdf `==6.14.2` | Selected, scoped | Native-text citation fidelity and explicit malformed/limit outcomes |
| OCR/layout | Tesseract `5.3.4` + `eng` tessdata | Selected, printed English only | Five cold/five warm quality, page provenance, RSS, package/model provenance |
| OCR/layout | Docling `==2.111.0` | Not selected | Corrected-corpus quality passed once, but repeated local execution exceeded host capacity |
| Object storage | S3-compatible | Provisional | Direct upload, checksum, lifecycle, residency, cost |
| Catalog | PostgreSQL 16+ | Provisional | State transactions, indexes, delete/status query plans |
| Broker | RabbitMQ quorum queues | Provisional | Confirms, redelivery, DLQ, backlog bounds, restart behavior |
| Execution | Celery `>=5.6,<5.7` | Evaluate | ID-only tasks, cancellation, retries, process containment |
| Search control | PostgreSQL + pgvector/GIN | Evaluate | Filtered recall, write/delete load, operational headroom |
| Embedding | FastEmbed `0.8.0` + multilingual MiniLM ONNX | Selected, local v1 | Baked model checksum, bounded batches, multilingual fixture relevance |
| Search | OpenSearch `2.19.5` hybrid | Selected, local v1 | Scoped hybrid relevance passed; P8 owns scale/cost validation |

Record exact versions, image digests, model hashes, environment shape, test
inputs, raw-result references, failures, and rejection rationale for every run.
