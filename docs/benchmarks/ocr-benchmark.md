# Opt-in OCR benchmark protocol

`docling==2.111.0` is isolated in the `ocr-benchmark` dependency group. It is
not part of the API runtime or normal development environment because its OCR
and layout stack is large and requires separately approved model artifacts.

The current local candidates are Docling with RapidOCR Torch, RapidOCR ONNX,
and EasyOCR. `onnxruntime` and `easyocr` are benchmark-only dependencies; they
are not available to the API runtime.

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

## Local execution

Prefetch the pinned model artifacts once, outside the timed run. The artifact
directory is ignored and must never contain customer documents:

```bash
uv run --group ocr-benchmark docling-tools models download \
  --output-dir benchmarks/ocr-artifacts layout tableformer rapidocr easyocr
uv run --group ocr-benchmark python benchmarks/run_docling_ocr_benchmark.py
```

The runner uses `HF_HUB_OFFLINE=1`, CPU-only inference, two inference threads,
an output-file ceiling, a 120-second worker timeout, and a fresh subprocess for
each process-cold sample. It records recursive artifact hashes/bytes, OCR text
recall, source-page provenance, duration, and peak RSS in the ignored results
file.

The benchmark host used on 2026-07-13 could run one Docling OCR worker (about
1.5–1.9 GiB peak RSS) but was killed when a second fresh Torch worker started.
RapidOCR Torch/ONNX and EasyOCR all produced non-English or near-empty output
for the synthetic English scan. These are candidate rejections, not a selected
OCR profile. Run the complete five-cold/five-warm matrix on an isolated host
with sufficient memory before selecting an OCR engine.

## Completion evidence for P1.5

P1.5 is done only after native and OCR candidates run against the approved
corpus under these controls, with cold/warm repetitions, page-citation quality,
resource/cost measurements, model provenance, and failure-injection results.
