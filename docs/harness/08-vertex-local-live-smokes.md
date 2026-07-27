# Vertex and local-compatible live smokes

Reviewed: 2026-07-27

These commands are operator-only. Tests never make live calls, and both paths
remain closed unless acknowledgement and a separate `enabled` environment gate
are present.

## Private inputs

Use one owner-only directory (`0700`). Configuration, route policy, and identity
files must be regular owner-only files (`0600`), use absolute paths, and contain
no credentials. Database and result paths must not exist before the command.

The input models are:

- provider inventory: `ProviderConfiguration`;
- local route and identity: `LocalCompatibleRoutePolicy` and
  `LocalCompatibleIdentityReference`;
- Vertex route and identity: `VertexRoutePolicy` and
  `VertexIdentityReference`.

A local bearer token, when required, stays in the exact environment variable
named by the identity and CLI. Vertex uses Application Default Credentials,
external-account credentials, or service-account impersonation; raw access
tokens in environment variables are rejected.

## Local capability probe

Run this first against the configured local server. It makes exactly six
sequential requests with no retries and writes destination/model-bound feature
evidence without response text.

```bash
ATLAS_LOCAL_PROBE_ENABLED=enabled \
uv run --locked python scripts/run_harness_local_probe.py \
  --acknowledge-live-probe \
  --model <configured-model> \
  --per-case-timeout-seconds 10 \
  --evidence-ttl-seconds 900 \
  --gate-environment-variable ATLAS_LOCAL_PROBE_ENABLED \
  --configuration-path /private/providers.json \
  --route-policy-path /private/local-route.json \
  --identity-path /private/local-identity.json \
  --result-path /private/local-capabilities.json
```

For bearer authentication, also pass
`--credential-environment-variable <identity-variable>`. Remote endpoints
require TLS and an explicit hostname allowlist; IP endpoints are loopback-only.

## Local one-call smoke

Use the fresh probe result before it expires. The local cost cap must be zero.

```bash
ATLAS_LOCAL_SMOKE_ENABLED=enabled \
uv run --locked python scripts/run_harness_adapter_smoke.py \
  --acknowledge-live-costs \
  --provider local-compatible \
  --model <configured-model> \
  --max-output-tokens 32 \
  --timeout-seconds 10 \
  --cost-cap-microusd 0 \
  --gate-environment-variable ATLAS_LOCAL_SMOKE_ENABLED \
  --configuration-path /private/providers.json \
  --route-policy-path /private/local-route.json \
  --identity-path /private/local-identity.json \
  --capability-evidence-path /private/local-capabilities.json \
  --database-path /private/local-smoke.sqlite3 \
  --result-path /private/local-smoke-result.json
```

Use the same bearer-variable argument as the probe when authentication is
configured.

## Vertex one-call smoke

Use a disposable project with billing budget/alerts and an identity whose quota
project and expected principal hashes match the route. The cost cap is an
operator-selected worst-case upper bound between 1 and 1,000,000 microusd.

```bash
ATLAS_VERTEX_SMOKE_ENABLED=enabled \
uv run --locked python scripts/run_harness_adapter_smoke.py \
  --acknowledge-live-costs \
  --provider vertex \
  --model <configured-model> \
  --max-output-tokens 32 \
  --timeout-seconds 10 \
  --cost-cap-microusd <approved-cap> \
  --disposable-project-id <disposable-project> \
  --gate-environment-variable ATLAS_VERTEX_SMOKE_ENABLED \
  --configuration-path /private/providers.json \
  --route-policy-path /private/vertex-route.json \
  --identity-path /private/vertex-identity.json \
  --database-path /private/vertex-smoke.sqlite3 \
  --result-path /private/vertex-smoke-result.json
```

The route must use one explicit non-global Vertex location and its exact
regional `aiplatform.googleapis.com` endpoint.

## Evidence check

Successful one-call results have the same canonical event shape and contain
only hashes, token counts, bounded cost, verification flags, and route binding:

```bash
jq '{provider, model, maximum_provider_calls, canonical_shape, event_count,
  output_sha256, canonical_events_sha256, route_binding_sha256,
  input_tokens, cached_input_tokens, output_tokens, reasoning_tokens,
  charged_cost_microusd, output_verified, active_credential_leases}' \
  /private/local-smoke-result.json \
  /private/vertex-smoke-result.json
```

Expected invariants are `maximum_provider_calls == 1`,
`canonical_shape == "text_usage_completed"`, `output_verified == true`, and
`active_credential_leases == 0`. The output text itself is never persisted.

## Sources

- https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/quickstart
- https://docs.cloud.google.com/vertex-ai/generative-ai/docs/reference/rpc/google.cloud.aiplatform.v1
- https://google-auth.readthedocs.io/en/latest/reference/google.auth.html
- https://google-auth.readthedocs.io/en/latest/reference/google.auth.impersonated_credentials.html
