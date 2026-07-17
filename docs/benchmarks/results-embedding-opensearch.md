# Multilingual embedding and OpenSearch selection

Status: selected local/on-prem v1 profile on 2026-07-17.

The selected embedding profile is FastEmbed `0.8.0` with
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, using the
Qdrant ONNX snapshot `faf4aa4225822f3bc6376869cb1164e8e3feedd0`. The model is
Apache-2.0 licensed, produces 384-dimensional vectors, and is published as a
multilingual model. The measured ONNX artifact is 235,052,644 bytes with
SHA-256 `634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99`.
Workers must use this baked artifact and verify its checksum; runtime model
downloads are not permitted.

The selected hybrid index is OpenSearch `2.19.5`, pinned by the image digest in
`benchmarks/infrastructure/compose.yaml`. Records carry exact tenant and
collection keywords, BM25 text, citation metadata, and the selected vector.
Application-side reciprocal-rank fusion uses `k=60` so lexical and vector
records remain independently auditable and deletable.

## Local evidence

Five warm trials ran on a 12-CPU Linux host against the immutable fixture
manifest and draft qrels. The FastEmbed 0.8.0 revalidation peak RSS was 710,180
KiB. Embedding the nine documents and six queries had p50 79.366 ms and p95
84.442 ms. Index build p50 was 264.810 ms and p95 was 469.479 ms; the largest
nine-record index used
177,547 bytes.

Vector search p95 was 18.311 ms and hybrid p95 was 27.033 ms. Lexical, vector,
and hybrid modes each achieved Recall@1/3/5, nDCG@1/3/5, MRR@5, citation
accuracy@1, and citation recall@5 of 1.0. Every result passed tenant and
collection isolation plus source-page metadata validation. The broker control
also passed persistent ID-only delivery, bounded prefetch, redelivery, DLQ,
overflow rejection, and graceful restart.

Raw local results are generated at
`benchmarks/results/infrastructure-multilingual-minilm.json` and are ignored by
Git. Reproduce with:

```bash
uv run --group infra-benchmark \
  python -m benchmarks.infrastructure.run_control_operations \
  --embedding-profile multilingual-minilm --trials 5 --broker-restart
```

## Limits

This fixture run proves local execution, deterministic profile identity,
filter isolation, citation preservation, and bounded small-corpus latency. It
is not a production-scale SLO. The primary scope has only five records, so
Recall@10 and nDCG@10 are not meaningful. P8 load tests must validate the
representative size distribution, queue saturation, peak RSS, index growth,
deletion, and recovery before production capacity claims are made.
