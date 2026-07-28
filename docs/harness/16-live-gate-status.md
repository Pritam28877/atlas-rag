# Live gate status

Checked: 2026-07-27. This record is intentionally not a pass claim.

## Environment evidence

- `GOOGLE_APPLICATION_CREDENTIALS`: absent.
- `AWS_PROFILE`: absent.
- No `ATLAS_LOCAL_*` live gate or configured local route was present.
- The environment contains an OpenAI key, but this gate requires Vertex and a
  real local OpenAI-compatible endpoint; no OpenAI call was made.

## Safe probe

The adapter smoke was invoked with the required local provider, a disabled
configuration path, a zero cost cap, and a separate gate variable. It returned
the generic `{"status":"failed"}` result with exit code `1` before a provider
request. No database or result artifact was created.

```text
uv run --locked python scripts/run_harness_adapter_smoke.py \
  --provider local-compatible --model missing --max-output-tokens 1 \
  --timeout-seconds 1 --cost-cap-microusd 0 \
  --gate-environment-variable ATLAS_LOCAL_SMOKE_ENABLED \
  --configuration-path /tmp/missing-providers.json \
  --route-policy-path /tmp/missing-route.json \
  --identity-path /tmp/missing-identity.json \
  --database-path /tmp/missing.sqlite3 \
  --result-path /tmp/missing-result.json
```

## Required next evidence

P14.3 and P15.3 remain `in_progress` until an operator supplies an explicit
loopback/local route and a disposable, budgeted GCP project. The required
receipts must contain the canonical event shape, usage/cost bounds,
cancellation result, route binding hash, and zero active credential leases.
