# Fixture-derived retrieval control

Status: local P1.6 control. This is reproducible plumbing evidence, not a
production search-provider selection or a relevance sign-off.

`benchmarks/retrieval/corpus-v1.json` contains only logical instances of
verified synthetic fixture pages. At runtime the control resolves the fixture
manifest, its page golden, source PDF SHA-256, and golden-file SHA-256. It
rejects non-search-eligible, non-`READY`, or non-scorable pages.

`qrels-v1.json` binds each query to a tenant and collection and supplies graded
relevance labels. The labels are explicitly draft and have not been
independently adjudicated. Each query scope includes at least one distractor;
identical source fixtures outside the scope are deliberate tenant/collection
leakage traps.

The current labels are single-relevant-item control qrels. They exercise metric
plumbing, scope isolation, and citation matching, but do not provide a
discriminating graded-relevance ranking evaluation.

The control indexes each snapshot into a disposable OpenSearch index and runs:

- BM25 lexical search with both scope filters;
- deterministic Unicode token-hash vector search with both scope filters;
- application-side reciprocal-rank fusion of fresh lexical/vector rankings.

Every returned hit must match the requested tenant and collection. The result
records independently aggregated scope isolation, source-metadata integrity,
top-result citation accuracy against qrels, citation recall at 5, Recall/nDCG
at 1, 3, and 5, MRR@5, and
per-mode p50/p95 latency. Recall@10 and nDCG@10 remain `null`: the primary
scope has only five source chunks, so depth ten is not meaningful.

The hash vector only validates vector indexing, filtering, and deterministic
reproducibility. It is not a semantic embedding candidate and cannot establish
English/Hindi relevance quality, cost, or model suitability. Before a selection
decision, use independently adjudicated qrels, a pinned real embedding model,
and a workload-scale snapshot with meaningful @10 metrics.
