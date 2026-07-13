# Opt-in OCR benchmark protocol

The selected P1 profile is Tesseract `5.3.4` with `eng` tessdata
`1:4.1.0-2`, rendered through Poppler `24.02.0` at 300 DPI. It runs only in a
dedicated OCR worker image, never in the API or native-parser worker.

`docling==2.111.0`, RapidOCR, ONNX Runtime, and EasyOCR remain opt-in
comparison dependencies only. They are not selected for P2.

## Dependency boundary

Install Tesseract, its English language pack, and Poppler in the dedicated
worker image during image build. Never download packages or models per job.
The local benchmark environment is resolved only when benchmarking:

```bash
uv sync --no-default-groups --group ocr-benchmark
```

Normal application work uses the lightweight development environment:

```bash
uv sync --group dev
```

## Required approval record

Before OCR converts a PDF, record the selected engine, language pack, package
versions, SHA-256, byte size, license, image digest, and hardware profile. The
selected `eng.traineddata` file is Apache-2.0 and has SHA-256
`7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2` on the
benchmark host.

## Required execution controls

- Benchmark only local, hash-verified synthetic or approved fixture paths.
- Install approved language data in a controlled build stage and use a
  read-only tessdata directory at runtime.
- Disable remote services, external plugins, picture description, VLM, formula,
  chart, and code enrichment unless individually approved and benchmarked.
- Run non-root, without outbound network, on a read-only filesystem except a
  bounded job temporary directory.
- Enforce outer CPU, memory, process, disk, and wall-time limits. Docling's own
  document timeout is insufficient by itself.
- Treat partial conversion, missing/corrupt model artifacts, OCR engine failure,
  unsupported language, timeout, and resource exhaustion as non-searchable
  terminal outcomes.

## Selected-profile benchmark

Run the selected profile against the hash-verified scan and mixed fixtures:

```bash
uv run --group ocr-benchmark python benchmarks/run_tesseract_ocr_benchmark.py
```

The runner renders each PDF page with Poppler and invokes a fresh Tesseract
process for every page. It records language-pack provenance, text recall,
source-page provenance, duration, and child-process RSS in an ignored result
file. Its cold and warm phases each run five repetitions.

On 2026-07-13, five cold and five warm repetitions produced ten scored fixture
runs each. Both phases had minimum token recall and page-citation coverage of
1.0; p95 duration was 1,491.762 ms cold and 1,496.726 ms warm; peak child RSS
was 126,300 KiB. The capacity calculation and limitations are in
[`ocr-selection-tesseract.md`](ocr-selection-tesseract.md).

The prior Docling rejection evidence was invalid because the synthetic English
scan was rendered with a Devanagari-only font and therefore contained missing
glyph boxes. The corpus is now revision `2026-07-13.1` and verifies a separate
Latin font for the scan. RapidOCR Torch reads the repaired fixture but needs
about 1.5 GiB RSS and this host cannot sustain repeated conversions. PaddleOCR
also fails local CPU inference in PaddlePaddle's oneDNN executor. Neither is
selected.

## Completion evidence for P1.5

P1.5 selection evidence consists of the native `pypdf` result, this five-cold/
five-warm OCR result, source-page provenance, bounded worker controls, and the
separate OCR capacity plan. This profile is intentionally limited to printed
English scans; other languages, handwriting, and complex layout require a new
benchmark and decision.
