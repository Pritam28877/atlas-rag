from __future__ import annotations

import argparse

import pytest

from benchmarks.infrastructure.benchmark_common import bounded_trials
from benchmarks.infrastructure.retrieval_control import _evaluate_hits, load_snapshot
from benchmarks.infrastructure.retrieval_metrics import evaluate_ranking


def test_fixture_derived_snapshot_has_scoped_distractors_and_qrels() -> None:
    documents, queries, _ = load_snapshot()
    by_scope: dict[tuple[str, str], int] = {}
    document_ids = {document["chunk_id"] for document in documents}
    for document in documents:
        scope = (document["tenant_id"], document["collection_id"])
        by_scope[scope] = by_scope.get(scope, 0) + 1

    for query in queries:
        scope = (query["tenant_id"], query["collection_id"])
        assert by_scope[scope] >= 2
        assert set(query["relevance"]) <= document_ids


def test_ranking_metrics_report_perfect_relevance() -> None:
    metrics = evaluate_ranking(["relevant", "other"], {"relevant": 3})

    assert metrics["recall_at_1"] == 1.0
    assert metrics["ndcg_at_1"] == 1.0
    assert metrics["mrr_at_5"] == 1.0
    assert metrics["recall_at_10"] is None


def test_non_relevant_top_hit_is_not_a_correct_citation() -> None:
    documents = {
        "relevant": {
            "source_sha256": "sha256:relevant",
            "page_number": 1,
        },
        "distractor": {
            "source_sha256": "sha256:distractor",
            "page_number": 1,
        },
    }
    query = {
        "query_id": "q",
        "tenant_id": "tenant-a",
        "collection_id": "collection-a",
        "relevance": {"relevant": 3},
    }
    hits = [
        {
            "_id": "distractor",
            "_source": {
                "tenant_id": "tenant-a",
                "collection_id": "collection-a",
                "source_sha256": "sha256:distractor",
                "page_number": 1,
            },
        }
    ]

    metrics = _evaluate_hits(hits, query, documents)

    assert metrics["scope_isolated"] is True
    assert metrics["metadata_integrity"] is True
    assert metrics["citation_accuracy_at_1"] is False
    assert metrics["citation_recall_at_5"] == 0.0


def test_trial_bound_is_enforced() -> None:
    assert bounded_trials("1") == 1
    assert bounded_trials("20") == 20
    with pytest.raises(argparse.ArgumentTypeError):
        bounded_trials("21")
