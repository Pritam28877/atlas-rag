"""Pure ranking metrics for the fixture-derived retrieval control."""

from __future__ import annotations

import math


def recall_at(ranked_ids: list[str], relevance: dict[str, int], limit: int) -> float:
    relevant_ids = {chunk_id for chunk_id, score in relevance.items() if score > 0}
    if not relevant_ids:
        raise ValueError("qrels must contain at least one relevant chunk")
    return len(set(ranked_ids[:limit]) & relevant_ids) / len(relevant_ids)


def ndcg_at(ranked_ids: list[str], relevance: dict[str, int], limit: int) -> float:
    actual = sum(
        (2 ** relevance.get(chunk_id, 0) - 1) / math.log2(rank + 2)
        for rank, chunk_id in enumerate(ranked_ids[:limit])
    )
    ideal_scores = sorted(relevance.values(), reverse=True)[:limit]
    ideal = sum(
        (2**score - 1) / math.log2(rank + 2) for rank, score in enumerate(ideal_scores)
    )
    return actual / ideal if ideal else 0.0


def reciprocal_rank_at(
    ranked_ids: list[str], relevance: dict[str, int], limit: int
) -> float:
    for rank, chunk_id in enumerate(ranked_ids[:limit], start=1):
        if relevance.get(chunk_id, 0) > 0:
            return 1 / rank
    return 0.0


def evaluate_ranking(
    ranked_ids: list[str], relevance: dict[str, int]
) -> dict[str, float | None]:
    metrics: dict[str, float | None] = {}
    for limit in (1, 3, 5):
        metrics[f"recall_at_{limit}"] = recall_at(ranked_ids, relevance, limit)
        metrics[f"ndcg_at_{limit}"] = ndcg_at(ranked_ids, relevance, limit)
    metrics["mrr_at_5"] = reciprocal_rank_at(ranked_ids, relevance, 5)
    metrics["recall_at_10"] = None
    metrics["ndcg_at_10"] = None
    return metrics
