# OCR selection: Tesseract English profile

## Decision

Select Tesseract `5.3.4` plus `eng` tessdata `1:4.1.0-2` for printed English
scan pages. PDFs are rendered by Poppler `24.02.0` at 300 DPI, then OCR runs in
a dedicated, network-isolated worker. The binary and tessdata packages are
Apache-2.0; `eng.traineddata` is 4,113,088 bytes with SHA-256
`7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2`.

## Evidence

On the synthetic corpus revision `2026-07-13.1`, the runner completed five
cold and five warm repetitions across the scan and mixed PDF fixtures (ten
scored records in each phase). Minimum token recall and source-page citation
coverage were both 1.0. Cold p95 was 1,491.762 ms, warm p95 was 1,496.726 ms,
and peak child RSS was 126,300 KiB.

## Capacity and isolation

One OCR worker has a 512 MiB memory limit and concurrency of one. This is more
than four times the measured RSS and leaves bounded space for Poppler, rendered
pages, and process overhead. At the observed worst-case 1.509-second fixture
duration, one worker has a synthetic lower-bound capacity of 39 OCR documents
per minute. Plan workers as:

```text
required_ocr_workers = ceil(peak_ocr_documents_per_minute / 39)
```

The OCR queue is separate from the native queue. It has independent retries,
queue-age alerts, worker memory limits, and concurrency, so scanned pages cannot
starve native parsing. Increase to at most two workers per host only after a
host-specific load test; use a 1,024 MiB hard cap per worker.

Core path: render selected PDF pages, then OCR each rendered page.
Time complexity: O(total rendered pixels).
Space complexity: O(largest rendered page) in the worker temporary directory.
Hot-path risks: high-resolution PDFs can inflate pixels and temporary storage.
Why this is acceptable: page, disk, timeout, output, memory, and concurrency
limits bound the work before a page enters OCR.

## Limitations

This decision does not approve handwriting, non-English language packs, tables,
reading-order reconstruction, or arbitrary image enhancement. Each requires a
new corpus class, model/package provenance, quality thresholds, and capacity
benchmark before it can enter the OCR allowlist.
