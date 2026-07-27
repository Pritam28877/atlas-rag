# Atlas Harness observability runbook

This runbook covers the disabled-by-default harness telemetry pipeline. It is
diagnostic evidence, not release approval.

## Controls

- Enable export only through the harness settings; the default is off.
- Redaction happens before queueing. Prompt, file, header, cookie, credential,
  token, and PII-like fields are replaced with `<redacted>`.
- Queue capacity is bounded at 4,096 events; one flush is at most 256 events.
- Export calls have a five-second timeout and at most two retries. Failed batches
  are dropped and counted; they never block a turn or grow memory.
- Metric names and labels come from a fixed catalog. User, tenant, thread, and
  document identifiers are not metric labels.

## Operator checks

Run from the repository root:

```bash
uv run --locked pytest -q tests/harness/observability
uv run --locked ruff check app/services/harness/observability tests/harness/observability
uv run --locked mypy
uv run --locked python scripts/verify_harness_python_boundaries.py
```

Evidence to record for each run:

| Field | Meaning |
| --- | --- |
| `enabled` | Whether export was explicitly enabled |
| `queue_size` | Current bounded queue depth |
| `exported` | Events accepted by the exporter |
| `dropped` | Overflow or failed-export count |
| `retries` | Attempts used for the last batch |
| `trace_id` | Opaque `trc_` correlation value, never a user identifier |

## Failure response

If redaction tests fail, disable export and preserve only the bounded local
diagnostic result. If export repeatedly fails, investigate the destination and
leave telemetry disabled; do not increase queue, batch, timeout, or retry caps.
Any metric-cardinality failure is a code/configuration issue and must be fixed
in the approved catalog before deployment.
