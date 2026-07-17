# Lifecycle hardening control results

Date: 2026-07-17. This is local engineering evidence, not a production
million-document capacity claim.

## Direct-service integration over real backing services

The opt-in `document_loader_e2e` test used PostgreSQL 18, MinIO, OpenSearch
2.19.5, and the pinned 384-dimensional FastEmbed model. It invoked the worker
services in-process with a recording dispatcher so failure and reconciliation
boundaries could be controlled deterministically. One real PDF was uploaded
through the API, normalized, published, reprocessed into a distinct `pdf-v2`
version, promoted, and deleted. The measured run completed in 6.43 seconds.
The old version had zero hits after promotion; the replacement had authorized
hits before deletion and zero afterward. The test also proves handoff failure,
duplicate index delivery, tenant denial, page citation, active-delete conflict,
catalog audit state, rollout-target cleanup, legal hold, and paged physical
cleanup under zero-day retention.

## Isolated-worker acceptance path

The opt-in `worker_integration` test used the same backing services plus a clean
RabbitMQ vhost and the built native, OCR, publication, lifecycle, and scheduler
containers. A scanned PDF traversed the API and every queue, produced an OCR
artifact, reached `ready`, and recorded succeeded preflight, OCR, chunk, embed,
and index jobs. A lifecycle request then removed its OpenSearch records and all
artifact rows through the lifecycle worker. The measured run completed in 9.19
seconds. Clean-vhost startup also showed all isolated consumers ready and the
beat-scheduled reconciliation task completing without delayed-delivery binding
errors.

The local acceptance target is under 15 seconds for this one-page warm-model
fixture and search revocation within the same delete worker attempt. This is a
regression gate only; production p95 and capacity must be established on target
hardware with representative customer data.

## Bounded control-plane load

Command, repeated five times:

```bash
uv run python benchmarks/infrastructure/lifecycle_load_control.py \
  --documents 10000 --workers 4 --queue-capacity 64
```

The harness derived its page distribution from all parseable repository PDF
fixtures (30,000 pages total per trial). It injected duplicate submissions,
controlled retry events, and deletion during indexing.

| Metric | Result |
|---|---:|
| Trials | 5 |
| Documents/pages per trial | 10,000 / 30,000 |
| Median control throughput | 261,411 documents/s |
| Worst reported p95 queue latency | 0.238 ms |
| Peak RSS across trials | 41,176 KiB |
| Queue configured/observed peak | 64 / 64 |
| Worker configured/observed active peak | 4 / 4 |
| Controlled retries per trial | 269 |
| Delete-during-index cases per trial | 190 |
| Duplicate searchable records | 0 |
| External provider cost | USD 0 |

The throughput measures only in-process control/backpressure work and must not be
used for parser, OCR, embedding, network, database, or OpenSearch sizing. The
cost is zero because the selected embedding model is local; hardware, storage,
operations, and energy costs were not measured. The production capacity gate is
therefore a target-environment load run using this same bounded ingress shape.

## Failure and recovery evidence

| Scenario | Evidence |
|---|---|
| Duplicate API/message/index work | Idempotency and continuous system tests; unique catalog constraints |
| Expired worker lease | Native integration and bounded reconciliation repair |
| Storage/checksum/model failure | Storage, native, OCR, and publication failure tests |
| Partial index/provider failure | Publication adapter retry tests; readiness remains gated |
| Tenant-filter bypass | Real OpenSearch system test returns zero for another tenant |
| Delete during indexing | Bounded control run plus deletion cancellation transaction |
| Search outage during delete | Delete service schedules a bounded retry after visibility call failure |
| Retention/legal hold | Search is revoked immediately; physical keys remain until policy permits |

## First scale limits

The first limits are worker CPU/RSS, OCR page time, OpenSearch indexing capacity,
and broker queue age—not Python queue memory. Ingress is bounded at the broker,
worker concurrency, embedding batch, document pages/chunks, reconciliation page,
and deletion batch. At one million documents, enqueue rate must remain below
measured target-environment service rate; this repository does not authorize a
one-million-message burst.
