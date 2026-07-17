# Document loader operations

This runbook covers the bounded workers and the database-backed queue signals
exported by `/v1/metrics`. Correlate an incident with job/version identifiers;
never paste document text, object keys, credentials, or tenant data into an
incident channel.

## Dashboard

The API metrics are database-backed control-plane job metrics for the native,
OCR, publication, and lifecycle stages. They are not RabbitMQ queue inspection.
Scrape RabbitMQ's Prometheus exporter separately for physical queue and DLQ state.

| Panel | PromQL | Warning | Critical |
|---|---|---:|---:|
| Queue depth | `rag_queue_depth` | sustained growth 15m | growth 30m or capacity exhausted |
| Oldest control-plane job | `rag_queue_oldest_age_seconds{queue!="dead-lettered-job"}` | 600s | 900s |
| Dead-lettered jobs | `rag_queue_depth{queue="dead-lettered-job"}` | nonzero 5m | sustained growth |
| RabbitMQ DLQ | `rabbitmq_queue_messages{queue="ingestion.dead-letter"}` | nonzero 5m | sustained growth |
| Metrics refresh | `rag_queue_metrics_refresh_success` | zero 5m | zero 15m |
| Reconciler heartbeat | `rag_scheduler_heartbeat_present`, `rag_scheduler_heartbeat_age_seconds` | missing or older than configured maximum | two consecutive alert windows |

Production Prometheus requests to `/v1/metrics` must send
`Authorization: Bearer <token>` from the scraper's secret store. The token is
configured as `TELEMETRY__METRICS_BEARER_TOKEN`; never place it in a URL, rule
file, dashboard, or log. Alert when the scrape target is down or the refresh
series is absent, because an initial PostgreSQL failure deliberately returns 503.
The shipped heartbeat rule uses the 180-second default; deployment templates
must render the configured `READINESS__SCHEDULER_HEARTBEAT_MAX_AGE_SECONDS`.

Use container-platform metrics for worker CPU/RSS and database queries over
`job_attempts`, `version_transition_events`, and `lifecycle_requests` for incident
analysis. Stage, lag, provider, and reconciliation metric families in the Python
telemetry helper are not exported across worker processes and must not be used in
alerts. Tenant, document, and job IDs never belong in metric labels.

## Retry and blocked stage

1. Confirm database, object storage, broker, and OpenSearch readiness.
2. Compare oldest queue age with worker concurrency and RSS/CPU limits.
3. Inspect sanitized `job_attempts` reason codes. Do not manually reset rows.
4. Restore the failed dependency, then invoke the bounded reconciliation task
   `app.workers.publication.reconcile`; it scans at most
   `LIFECYCLE__RECONCILE_PAGE_SIZE` rows and republishes idempotent jobs.
5. Escalate if the same job reaches its configured attempt limit.

## Dead-letter investigation

First distinguish a database `dead_lettered` job from a RabbitMQ DLQ message.
Pause new ingress for the affected stage if either depth is increasing. Classify
the sanitized reason code, preserve the broker message identifier and catalog
IDs, remediate the dependency or input-policy issue, then replay through the
lifecycle API or the reconciler. Never republish arbitrary message bodies or
bypass schema validation.

## Provider outage or throttling

Keep concurrency at the configured bound, verify the provider health and sanitized
worker failure reason, and allow exponential retries. Do not increase retries
without a capacity review.
If the pinned model or checksum is unavailable, restore the exact approved model;
do not silently substitute another embedding profile.

## Parser/OCR incident

Stop only the affected worker pool. Confirm the container CPU/RSS/pid limits and
temporary-directory capacity. Quarantine malformed or unsupported input; never
open it outside the isolated worker. Reprocess with an explicitly new profile only
after a normalized artifact is available.

## Reprocess and promotion

Submit `operation=reprocess` with an explicit new `pipeline_profile`. The API
creates a distinct version linked by `reprocessed_from_version_id`; old artifacts
and search results remain until the replacement has complete chunk, embedding,
and index evidence. Promotion activates the replacement, removes the old search
records, marks old publications deleted, and supersedes the old catalog version.

## Deletion incident

Deletion atomically hides the catalog version, revokes its publication rows, and
records durable tenant/version-scoped OpenSearch cleanup before making an external
call. The worker then removes search records and physically removes catalog-owned
objects only when retention permits. Legal hold keeps bytes but never restores
search visibility. If delete lag exceeds five minutes, run reconciliation, verify
OpenSearch health, and query only by tenant/collection/version IDs to confirm zero
visible hits. Retention-delayed cleanup is recreated by reconciliation.

## Recovery and rollback

All repair dispatches are idempotent and bounded. RabbitMQ rejects changed
quorum-queue arguments on an existing queue. To change queue bounds, first stop
write traffic, API publishers, reconciliation Beat, and every old worker. Inspect
all five queues and preserve their message counts. Drain the four work queues,
delete only queues proven empty, then let one new worker declare the topology.
Never delete a nonempty queue.

If the DLQ is nonempty, give the replacement a versioned
`BROKER__DEAD_LETTER_QUEUE_NAME`, unbind the old DLQ while all publishers are
stopped, and retain the old queue for audited replay. Delete the old DLQ only
after it is empty and the replay has been verified. If the DLQ is already empty,
it may be deleted and redeclared with the same name. Resume Beat, workers, API,
and ingress only after declarations and message counts are verified.

Roll back application code before rolling back schema. Migration `20260717_05`
must be downgraded before `20260717_04`, and neither downgrade is safe while active
publication recovery or linked reprocessed versions remain. Restore catalog and
storage from the same recovery point, then reconcile before reopening ingress.
