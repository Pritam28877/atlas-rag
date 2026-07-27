# Cross-provider live matrix

Reviewed: 2026-07-27

This runbook aggregates redacted smoke receipts. It does not authorize live
calls. Each provider command still requires its own acknowledgement, gate,
identity, route policy, and cost cap.

## Release set

The matrix names mock, OpenAI, OpenRouter, Bedrock, Vertex, and the local
OpenAI-compatible adapter. Release evidence requires these three providers:

- `bedrock`: cloud cancellation, text, tools, and usage;
- `vertex`: cloud cancellation, text, and usage;
- `local-compatible`: local cancellation, text, and usage.

OpenAI and OpenRouter receipts currently prove text and usage only, so they
cannot replace a release-set provider. Missing evidence remains `untested`;
unsupported adapter scenarios remain `unsupported`; a bound failure receipt
becomes `failed`.

## 1. Prepare private paths

Use one canonical owner-only directory and owner-only input files. Credentials
stay in the explicitly named environment or workload identity.

```bash
install -d -m 0700 /private/atlas-live-matrix
umask 077
```

Every input and receipt must be a regular `0600` file. Database, smoke-result,
and final matrix paths must not exist before their commands run. Do not put
credentials, prompts, outputs, or signing keys in the matrix manifest.

## 2. Bind provider descriptors

Generate the verified offline `ConformanceReport` from packaged,
checksum-pinned recordings:

```bash
uv run --locked python scripts/run_harness_conformance.py \
  --timeout-seconds 30 \
  --result-path /private/atlas-live-matrix/conformance.json
```

The command makes no network calls. It writes a new owner-only report only when
all supported cases pass and every eligible comparison is equivalent; stdout
contains only status and dimension counts.

Create the six sorted `LiveProviderTarget` records from the approved provider
configuration; do not add adapter revisions or supported scenarios.

Each target supplies:

- provider environment: `cloud`, `local`, or `mock`;
- configured model and its revision SHA-256;
- smoke route-binding SHA-256;
- observation time.

The preparation command in step 4 derives descriptors from the report. It
rejects a target set that does not exactly match the conformance adapters and
carries forward their revisions and supported scenarios.

## 3. Produce redacted receipts

Run only the providers explicitly authorized by the operator.

- OpenAI and OpenRouter: use the bounded one-call command below.
- Bedrock: follow [07-bedrock-converse-evidence.md](07-bedrock-converse-evidence.md).
- Vertex and local: follow [08-vertex-local-live-smokes.md](08-vertex-local-live-smokes.md).

OpenAI:

```bash
uv run --locked python scripts/run_harness_provider_smoke.py \
  --acknowledge-live-costs \
  --provider openai \
  --model <configured-model> \
  --max-output-tokens 16 \
  --timeout-seconds 10 \
  --cost-cap-microusd <approved-cap> \
  --config-path /private/providers.json \
  --database-path /private/atlas-live-matrix/openai.sqlite3 \
  --result-path /private/atlas-live-matrix/openai-result.json \
  --destination-url <approved-responses-url> \
  --environment-variable <openai-key-variable>
```

OpenRouter uses the same command with `--provider openrouter`, unique database
and result paths, its approved destination and credential variable, plus:

```text
--openrouter-policy-path /private/openrouter-policy.json
```

Never edit a receipt to turn a failed or missing run into passed evidence.
Authorized failures may be recorded only as revision- and route-bound
`LiveSmokeFailureReceipt` objects.

## 4. Create the manifest

Write `/private/atlas-live-matrix/preparation.json` as a
`LiveMatrixPreparationRequest` and set it to `0600`. Its fields are:

- `schema_version`: `1`;
- `targets`: six sorted approved provider targets from step 2;
- `evidence`: sorted references to existing private receipts;
- `required_live_providers`:
  `["bedrock", "local-compatible", "vertex"]`;
- `authorized_cost_cap_microusd`: the approved total cap for all receipts;
- `result_path`: a new absolute path for the matrix.

Evidence kinds are:

| Provider | Kind |
| --- | --- |
| Bedrock | `bedrock` |
| local-compatible, Vertex | `adapter` |
| OpenAI, OpenRouter | `provider` |
| Any bound failed attempt | `failure` |

The mock provider has no live receipt. Omit any optional provider that was not
run. Report, preparation, manifest, receipt, and output paths must be distinct
absolute paths.

Derive and create the owner-only manifest:

```bash
uv run --locked python scripts/prepare_harness_live_matrix.py \
  --conformance-report-path /private/atlas-live-matrix/conformance.json \
  --preparation-path /private/atlas-live-matrix/preparation.json \
  --manifest-path /private/atlas-live-matrix/manifest.json
```

Success prints only `status` and the provider count. Invalid or permissive
inputs, mismatched target sets, path collisions, and existing output paths fail
without a manifest.

## 5. Build the matrix

```bash
uv run --locked python scripts/build_harness_live_matrix.py \
  --manifest-path /private/atlas-live-matrix/manifest.json
```

Success prints only `status` and `release_ready`. Failure prints a generic
status, writes no replacement output, and exits nonzero. The resulting matrix
is created as an owner-only file.

## 6. Verify release evidence

```bash
jq '{
  required_live_providers,
  authorized_cost_cap_microusd,
  charged_cost_microusd,
  release_ready
}' /private/atlas-live-matrix/matrix.json

jq -e '
  .charged_cost_microusd <= .authorized_cost_cap_microusd
  and .release_ready == true
' /private/atlas-live-matrix/matrix.json

jq '
  .required_live_providers as $required
  | [
      .observations[]
      | select(.provider as $provider | $required | index($provider))
      | {provider, scenario, status}
    ]
' /private/atlas-live-matrix/matrix.json
```

The final command must show `passed` for cancellation, text, and usage on all
three required providers. Review other cells as `passed`, `failed`,
`unsupported`, or `untested`; never infer support from a missing receipt.

No live provider smoke or release matrix has been executed in this repository.
