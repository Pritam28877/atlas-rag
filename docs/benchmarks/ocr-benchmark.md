# Opt-in OCR benchmark protocol

`docling==2.111.0` is isolated in the `ocr-benchmark` dependency group. It is
not part of the API runtime or normal development environment because its OCR
and layout stack is large and requires separately approved model artifacts.

## Dependency boundary

Resolve the OCR benchmark environment only when benchmarking:

```bash
uv sync --no-default-groups --group ocr-benchmark
```

Normal application work uses the lightweight development environment:

```bash
uv sync --group dev
```

## Required approval record

Before Docling converts a PDF, record the selected OCR engine, language packs,
model repository revision, SHA-256, byte size, model license, legal approval,
and hardware profile. The Docling package license is not approval for every
model or OCR engine it can use.

## Required execution controls

- Benchmark only local, hash-verified synthetic or approved fixture paths.
- Pre-fetch approved model artifacts in a controlled build stage; benchmark with
  `HF_HUB_OFFLINE=1` and an explicit read-only local artifact path.
- Disable remote services, external plugins, picture description, VLM, formula,
  chart, and code enrichment unless individually approved and benchmarked.
- Run non-root, without outbound network, on a read-only filesystem except a
  bounded job temporary directory.
- Enforce outer CPU, memory, process, disk, and wall-time limits. Docling's own
  document timeout is insufficient by itself.
- Treat partial conversion, missing/corrupt model artifacts, OCR engine failure,
  unsupported language, timeout, and resource exhaustion as non-searchable
  terminal outcomes.

## Completion evidence for P1.5

P1.5 is done only after native and OCR candidates run against the approved
corpus under these controls, with cold/warm repetitions, page-citation quality,
resource/cost measurements, model provenance, and failure-injection results.

