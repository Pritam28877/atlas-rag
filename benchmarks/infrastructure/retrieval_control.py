"""Fixture-derived lexical, vector, and harness-level hybrid retrieval control."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

try:
    from .benchmark_common import elapsed, request_json, require
    from .embedding_profiles import EmbeddingProfile, TokenHashEmbeddingProfile
    from .retrieval_metrics import evaluate_ranking
except ImportError:  # Direct script execution keeps this benchmark self-contained.
    from benchmark_common import elapsed, request_json, require
    from embedding_profiles import EmbeddingProfile, TokenHashEmbeddingProfile
    from retrieval_metrics import evaluate_ranking

ROOT = Path(__file__).resolve().parents[2]
CORPUS_PATH = ROOT / "benchmarks/retrieval/corpus-v1.json"
QRELS_PATH = ROOT / "benchmarks/retrieval/qrels-v1.json"
RRF_K = 60


def token_hash_embedding(text: str) -> list[float]:
    """Compatibility helper retained for the original plumbing-control tests."""
    return TokenHashEmbeddingProfile().embed([text])[0]


def _sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _repo_path(reference: str) -> Path:
    path = (ROOT / reference).resolve()
    if ROOT not in path.parents:
        raise ValueError(f"benchmark reference escapes repository: {reference}")
    return path


def load_snapshot(
    embedding_profile: EmbeddingProfile | None = None,
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], dict[str, str]
]:
    profile = embedding_profile or TokenHashEmbeddingProfile()
    manifest = json.loads(
        (ROOT / "docs/benchmarks/fixture-manifest.json").read_text(encoding="utf-8")
    )
    fixtures = {fixture["id"]: fixture for fixture in manifest["fixtures"]}
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    qrels = json.loads(QRELS_PATH.read_text(encoding="utf-8"))
    if qrels["corpus_id"] != corpus["id"]:
        raise ValueError("qrels corpus_id does not match corpus")

    documents: list[dict[str, Any]] = []
    for instance in corpus["instances"]:
        fixture = fixtures[instance["fixture_id"]]
        outcome = fixture["expected_outcome"]
        if "search" not in fixture["benchmark_eligibility"]:
            raise ValueError(f"fixture is not search eligible: {fixture['id']}")
        if outcome["terminal_state"] != "READY" or not outcome["golden_ref"]:
            raise ValueError(f"fixture is not search-ready: {fixture['id']}")
        source_path = _repo_path(fixture["artifact"]["object_ref"])
        if (
            not source_path.is_file()
            or source_path.stat().st_size != fixture["artifact"]["bytes"]
        ):
            raise ValueError(
                f"fixture artifact does not match manifest: {fixture['id']}"
            )
        if _sha256(source_path) != fixture["artifact"]["sha256"]:
            raise ValueError(
                f"fixture checksum does not match manifest: {fixture['id']}"
            )
        golden_path = _repo_path(outcome["golden_ref"])
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
        if golden["fixture_id"] != fixture["id"]:
            raise ValueError(f"golden fixture ID does not match: {fixture['id']}")
        page = next(
            item
            for item in golden["pages"]
            if item["page_number"] == instance["page_number"]
        )
        if not page["scorable_by_native_parser"]:
            raise ValueError(f"page is not native-search scorable: {fixture['id']}")
        documents.append(
            {
                **instance,
                "body": page["text"],
                "source_sha256": fixture["artifact"]["sha256"],
                "golden_sha256": _sha256(golden_path),
            }
        )
    vectors = profile.embed([document["body"] for document in documents])
    if len(vectors) != len(documents):
        raise ValueError("embedding profile returned an incomplete document batch")
    for document, vector in zip(documents, vectors, strict=True):
        if len(vector) != profile.dimension:
            raise ValueError("embedding dimension does not match profile")
        document["embedding"] = vector
    _validate_queries(documents, qrels["queries"])
    return (
        documents,
        qrels["queries"],
        {
            "fixture_manifest_version": manifest["corpus"]["immutable_version"],
            "corpus_sha256": _sha256(CORPUS_PATH),
            "qrels_sha256": _sha256(QRELS_PATH),
        },
    )


def _validate_queries(
    documents: list[dict[str, Any]], queries: list[dict[str, Any]]
) -> None:
    by_id = {document["chunk_id"]: document for document in documents}
    for query in queries:
        scope = (query["tenant_id"], query["collection_id"])
        scoped = [
            document
            for document in documents
            if (document["tenant_id"], document["collection_id"]) == scope
        ]
        if len(scoped) < 2:
            raise ValueError(f"query scope needs a distractor: {query['query_id']}")
        for chunk_id in query["relevance"]:
            document = by_id.get(chunk_id)
            if document is None:
                raise ValueError(f"qrel refers to unknown chunk: {chunk_id}")
            if (document["tenant_id"], document["collection_id"]) != scope:
                raise ValueError(f"qrel crosses scope: {query['query_id']}")


def _search(
    values: dict[str, str], index: str, query: dict[str, Any]
) -> tuple[list[dict[str, Any]], float]:
    started = time.perf_counter()
    response = request_json(
        values, "POST", f"/{index}/_search", {"size": 10, "query": query}
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    return response["hits"]["hits"], latency_ms


def _scope_filter(query: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"term": {"tenant_id": query["tenant_id"]}},
        {"term": {"collection_id": query["collection_id"]}},
    ]


def _rrf(*rankings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    documents: dict[str, dict[str, Any]] = {}
    for ranking in rankings:
        for rank, document in enumerate(ranking, start=1):
            chunk_id = document["_id"]
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (RRF_K + rank)
            documents[chunk_id] = document
    ordered_ids = sorted(
        scores,
        key=lambda chunk_id: scores[chunk_id],
        reverse=True,
    )
    return [documents[chunk_id] for chunk_id in ordered_ids]


def _evaluate_hits(
    hits: list[dict[str, Any]],
    query: dict[str, Any],
    documents: dict[str, dict[str, Any]],
) -> dict[str, float | bool | None]:
    for hit in hits:
        source = hit["_source"]
        if source["tenant_id"] != query["tenant_id"]:
            raise AssertionError(f"tenant leakage for {query['query_id']}")
        if source["collection_id"] != query["collection_id"]:
            raise AssertionError(f"collection leakage for {query['query_id']}")
    ranked_ids = [hit["_id"] for hit in hits]
    metrics = evaluate_ranking(ranked_ids, query["relevance"])
    metadata_integrity = all(
        documents[chunk_id]["source_sha256"] == hit["_source"]["source_sha256"]
        and documents[chunk_id]["page_number"] == hit["_source"]["page_number"]
        for chunk_id, hit in ((hit["_id"], hit) for hit in hits)
    )
    require(metadata_integrity, "indexed citation metadata did not match the snapshot")
    relevant_ids = set(query["relevance"])
    cited_relevant = set(ranked_ids[:5]) & relevant_ids
    return {
        **metrics,
        "scope_isolated": True,
        "metadata_integrity": metadata_integrity,
        "citation_accuracy_at_1": bool(ranked_ids and ranked_ids[0] in relevant_ids),
        "citation_recall_at_5": len(cited_relevant) / len(relevant_ids),
    }


def run_retrieval_control(
    values: dict[str, str],
    run_id: str,
    embedding_profile: EmbeddingProfile | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    profile = embedding_profile or TokenHashEmbeddingProfile()
    embedding_started = time.perf_counter()
    documents, queries, snapshot = load_snapshot(profile)
    query_vectors = profile.embed([query["text"] for query in queries])
    embedding_ms = round((time.perf_counter() - embedding_started) * 1000, 3)
    if len(query_vectors) != len(queries):
        raise ValueError("embedding profile returned an incomplete query batch")
    document_by_id = {document["chunk_id"]: document for document in documents}
    index = f"atlas-rag-retrieval-{run_id}"

    def build_index() -> None:
        request_json(
            values,
            "PUT",
            f"/{index}",
            {
                "settings": {"index.knn": True},
                "mappings": {
                    "properties": {
                        "tenant_id": {"type": "keyword"},
                        "collection_id": {"type": "keyword"},
                        "body": {"type": "text"},
                        "page_number": {"type": "integer"},
                        "source_sha256": {"type": "keyword"},
                        "embedding": {
                            "type": "knn_vector",
                            "dimension": profile.dimension,
                        },
                    }
                },
            },
        )
        for document in documents:
            request_json(
                values,
                "PUT",
                f"/{index}/_doc/{document['chunk_id']}?refresh=true",
                document,
            )

    try:
        build_ms = elapsed(build_index)
        records: list[dict[str, Any]] = []
        for query, query_vector in zip(queries, query_vectors, strict=True):
            filters = _scope_filter(query)
            lexical, lexical_ms = _search(
                values,
                index,
                {
                    "bool": {
                        "filter": filters,
                        "must": [{"match": {"body": query["text"]}}],
                    }
                },
            )
            vector, vector_ms = _search(
                values,
                index,
                {
                    "knn": {
                        "embedding": {
                            "vector": query_vector,
                            "k": 10,
                            "filter": {"bool": {"filter": filters}},
                        }
                    }
                },
            )
            hybrid_started = time.perf_counter()
            hybrid_lexical, _ = _search(
                values,
                index,
                {
                    "bool": {
                        "filter": filters,
                        "must": [{"match": {"body": query["text"]}}],
                    }
                },
            )
            hybrid_vector, _ = _search(
                values,
                index,
                {
                    "knn": {
                        "embedding": {
                            "vector": query_vector,
                            "k": 10,
                            "filter": {"bool": {"filter": filters}},
                        }
                    }
                },
            )
            hybrid_ms = round((time.perf_counter() - hybrid_started) * 1000, 3)
            for mode, hits, latency_ms in (
                ("lexical", lexical, lexical_ms),
                ("vector", vector, vector_ms),
                ("hybrid_rrf", _rrf(hybrid_lexical, hybrid_vector), hybrid_ms),
            ):
                records.append(
                    {
                        "query_id": query["query_id"],
                        "mode": mode,
                        "latency_ms": latency_ms,
                        "ranked_chunk_ids": [hit["_id"] for hit in hits],
                        "metrics": _evaluate_hits(hits, query, document_by_id),
                    }
                )
        stats = request_json(values, "GET", f"/{index}/_stats/store")
        return (
            {
                "build_ms": build_ms,
                "index_storage_bytes": stats["_all"]["primaries"]["store"][
                    "size_in_bytes"
                ],
                "corpus_documents": len(documents),
                "query_count": len(queries),
                "embedding_ms": embedding_ms,
                "embedding_profile": profile.name,
                "embedding_model_version": profile.model_version,
                "embedding_dimension": profile.dimension,
                "embedding_license": profile.license_name,
                "snapshot": snapshot,
            },
            records,
        )
    finally:
        try:
            request_json(values, "DELETE", f"/{index}")
        except (HTTPError, URLError, ConnectionResetError):
            pass
