"""Pure deterministic judgment pipeline."""

from __future__ import annotations

from typing import Any

from .aggregate import aggregate_messages, verify_aggregation
from .errors import fail
from .normalize import normalize_text
from .risk import merge_risk_candidates, scan_risk_hits, validate_result_risk_hits
from .sentiment import score_sentiment
from .util import message_ids


def judge_conversation(
    conversation: dict[str, Any],
    config: dict[str, Any],
    risk_words: dict[str, Any],
    batch_no: int,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for source in sorted(conversation["messages"], key=lambda item: int(item["msg_no"])):
        normalized = normalize_text(source["text"])
        sentiment = score_sentiment(normalized, config)
        risk_hits = scan_risk_hits(int(source["msg_no"]), normalized, risk_words)
        messages.append(
            {
                "conv_id": conversation["conv_id"],
                "msg_no": int(source["msg_no"]),
                "speaker": source["speaker"],
                "text": source["text"],
                "locale": source["locale"],
                "annotator_group": source["annotator_group"],
                "weight": float(source.get("weight", 1.0)),
                "normalized_text": normalized,
                "sentiment_label": sentiment["sentiment_label"],
                "sentiment_scores": sentiment["sentiment_scores"],
                "sentiment_score": sentiment["sentiment_score"],
                "sentiment_terms": sentiment["sentiment_terms"],
                "risk_hits": risk_hits,
            }
        )

    aggregate = aggregate_messages(messages, config, risk_words)
    result = {
        "conv_id": conversation["conv_id"],
        "batch_no": int(batch_no),
        "locale": conversation["locale"],
        "annotator_group": conversation["annotator_group"],
        "message_ids": message_ids(conversation),
        "message_count": len(messages),
        "messages": messages,
        **aggregate,
    }
    validate_result_risk_hits(result, risk_words)
    verify_aggregation(result, config, risk_words)
    return result


def judge_all(
    conversations: dict[str, dict[str, Any]],
    config: dict[str, Any],
    risk_words: dict[str, Any],
    batch_size: int | None = None,
) -> tuple[dict[str, dict[str, Any]], int]:
    size = int(batch_size if batch_size is not None else config["batch_size"])
    if size <= 0:
        fail("E_CONFIG", "batch size must be positive", batch_size=size)
    conv_ids = sorted(conversations)
    results: dict[str, dict[str, Any]] = {}
    batch_count = 0
    for offset in range(0, len(conv_ids), size):
        batch_no = offset // size
        batch_count = batch_no + 1
        for conv_id in conv_ids[offset : offset + size]:
            results[conv_id] = judge_conversation(
                conversations[conv_id], config, risk_words, batch_no
            )
    return results, batch_count


def empty_risk_candidates(risk_words: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {category: [] for category in risk_words["categories"]}


def merged_candidates_for(
    result: dict[str, Any], risk_words: dict[str, Any]
) -> list[dict[str, Any]]:
    return merge_risk_candidates(result["messages"], risk_words)

