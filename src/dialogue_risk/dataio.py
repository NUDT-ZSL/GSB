"""Loading and validation for deterministic local input files."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .errors import fail
from .normalize import normalize_text


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        fail("E_INPUT", "required file does not exist", path=str(path))
    except (OSError, json.JSONDecodeError) as exc:
        fail("E_INPUT", "cannot read JSON file", path=str(path), reason=str(exc))


def _require_int(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        fail("E_CONFIG", f"{name} must be an integer >= {minimum}", value=value)
    return value


def _require_number(value: Any, name: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail("E_CONFIG", f"{name} must be numeric", value=value)
    result = float(value)
    if not math.isfinite(result):
        fail("E_CONFIG", f"{name} must be finite", value=value)
    if minimum is not None and result < minimum:
        fail("E_CONFIG", f"{name} must be >= {minimum}", value=value)
    return result


def validate_config(config: dict[str, Any]) -> None:
    if config.get("schema_version") != 1:
        fail("E_CONFIG", "unsupported config schema", schema_version=config.get("schema_version"))
    _require_int(config.get("config_version"), "config_version", 1)

    versions = config.get("versions")
    if not isinstance(versions, dict):
        fail("E_CONFIG", "versions object is required")
    _require_int(versions.get("results"), "versions.results", 1)
    _require_int(versions.get("weights"), "versions.weights", 1)

    precision = config.get("precision")
    if not isinstance(precision, dict):
        fail("E_CONFIG", "precision object is required")
    decimals = _require_int(precision.get("decimals"), "precision.decimals", 0)
    if decimals > 9:
        fail("E_CONFIG", "precision.decimals must be <= 9")
    if not isinstance(precision.get("flag"), str) or not precision["flag"]:
        fail("E_CONFIG", "precision.flag is required")

    sentiment = config.get("sentiment")
    if not isinstance(sentiment, dict):
        fail("E_CONFIG", "sentiment object is required")
    order = sentiment.get("label_order")
    if order != ["negative", "neutral", "positive"]:
        fail(
            "E_CONFIG",
            "sentiment.label_order must be negative, neutral, positive",
            value=order,
        )
    weights = sentiment.get("category_weights")
    if not isinstance(weights, dict) or set(weights) != set(order):
        fail("E_CONFIG", "sentiment category weights must cover all labels")
    for label in order:
        _require_number(weights[label], f"sentiment.category_weights.{label}", 0.0)
    _require_number(sentiment.get("neutral_baseline"), "sentiment.neutral_baseline", 0.0)
    lexicon = sentiment.get("lexicon")
    if not isinstance(lexicon, dict) or not lexicon:
        fail("E_CONFIG", "sentiment.lexicon must be a non-empty object")
    for term, score in lexicon.items():
        if not term or normalize_text(term) != term:
            fail("E_CONFIG", "sentiment terms must already be normalized", term=term)
        _require_number(score, f"sentiment.lexicon.{term}")

    context = config.get("context")
    if not isinstance(context, dict):
        fail("E_CONFIG", "context object is required")
    negations = context.get("negations")
    if not isinstance(negations, list) or not negations:
        fail("E_CONFIG", "context.negations must be a non-empty list")
    if len(negations) != len(set(negations)):
        fail("E_CONFIG", "context.negations contains duplicates")
    for term in negations:
        if not isinstance(term, str) or not term or normalize_text(term) != term:
            fail("E_CONFIG", "context.negations terms must be normalized", term=term)
    degree_adverbs = context.get("degree_adverbs")
    if not isinstance(degree_adverbs, dict) or not degree_adverbs:
        fail("E_CONFIG", "context.degree_adverbs must be a non-empty object")
    for term, multiplier in degree_adverbs.items():
        if not isinstance(term, str) or not term or normalize_text(term) != term:
            fail(
                "E_CONFIG",
                "context.degree_adverbs terms must be normalized",
                term=term,
            )
        _require_number(multiplier, f"context.degree_adverbs.{term}", 0.0)
    _require_int(context.get("lookback_codepoints"), "context.lookback_codepoints", 1)

    aggregation = config.get("aggregation")
    if not isinstance(aggregation, dict):
        fail("E_CONFIG", "aggregation object is required")
    decay = aggregation.get("decay")
    if not isinstance(decay, dict) or not isinstance(decay.get("enabled"), bool):
        fail("E_CONFIG", "aggregation.decay.enabled is required")
    _require_number(decay.get("half_life_steps"), "aggregation.decay.half_life_steps", 0.01)
    _require_number(aggregation.get("risk_score_cap"), "aggregation.risk_score_cap", 0.0)
    category_weights = aggregation.get("risk_category_weights")
    if not isinstance(category_weights, dict):
        fail("E_CONFIG", "aggregation.risk_category_weights is required")
    for category, weight in category_weights.items():
        _require_number(weight, f"aggregation.risk_category_weights.{category}", 0.0)
    levels = aggregation.get("risk_levels")
    if not isinstance(levels, list) or not levels:
        fail("E_CONFIG", "aggregation.risk_levels is required")
    previous = None
    seen = set()
    for level in levels:
        if not isinstance(level, dict) or not isinstance(level.get("label"), str):
            fail("E_CONFIG", "each risk level needs a label")
        label = level["label"]
        if label in seen:
            fail("E_CONFIG", "risk level labels must be unique", label=label)
        seen.add(label)
        minimum = _require_number(
            level.get("min_score"), f"aggregation.risk_levels.{label}.min_score", 0.0
        )
        if previous is not None and minimum > previous:
            fail("E_CONFIG", "risk levels must be ordered by descending min_score")
        previous = minimum

    _require_int(config.get("batch_size"), "batch_size", 1)

    cache = config.get("cache")
    if not isinstance(cache, dict) or not isinstance(cache.get("enabled"), bool):
        fail("E_CONFIG", "cache.enabled is required")
    _require_int(cache.get("max_entries"), "cache.max_entries", 1)

    limits = config.get("resource_limits")
    if not isinstance(limits, dict):
        fail("E_CONFIG", "resource_limits is required")
    for field_name in (
        "max_conversations",
        "max_messages",
        "max_cache_entries",
    ):
        _require_int(limits.get(field_name), f"resource_limits.{field_name}", 1)

    query = config.get("query")
    if not isinstance(query, dict):
        fail("E_CONFIG", "query object is required")
    _require_number(query.get("latency_budget_ms"), "query.latency_budget_ms", 0.0)


def validate_risk_words(risk_words: dict[str, Any]) -> None:
    if risk_words.get("schema_version") != 1:
        fail(
            "E_CONFIG",
            "unsupported risk word schema",
            schema_version=risk_words.get("schema_version"),
        )
    _require_int(risk_words.get("version"), "risk_words.version", 1)
    categories = risk_words.get("categories")
    if not isinstance(categories, list) or not categories:
        fail("E_CONFIG", "risk word categories are required")
    if len(categories) != len(set(categories)) or any(
        not isinstance(category, str) or not category for category in categories
    ):
        fail("E_CONFIG", "risk categories must be unique non-empty strings")
    terms = risk_words.get("terms")
    if not isinstance(terms, dict) or set(terms) != set(categories):
        fail("E_CONFIG", "risk term categories must exactly match controlled categories")
    for category in categories:
        entries = terms[category]
        if not isinstance(entries, list) or not entries:
            fail("E_CONFIG", "risk term category must be a non-empty list", category=category)
        seen_terms: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                fail("E_CONFIG", "risk term entry must be an object", category=category)
            term = entry.get("term")
            if (
                not isinstance(term, str)
                or not term
                or normalize_text(term) != term
                or term in seen_terms
            ):
                fail(
                    "E_CONFIG",
                    "risk terms must be normalized and unique within a category",
                    category=category,
                    term=term,
                )
            seen_terms.add(term)
            _require_number(entry.get("severity"), f"risk.{category}.{term}.severity", 0.0)


def validate_conversations(conversations: dict[str, dict[str, Any]]) -> None:
    for conv_id, conversation in conversations.items():
        if conversation.get("conv_id") != conv_id:
            fail("E_INPUT", "conversation key does not match conv_id", conv_id=conv_id)
        messages = conversation.get("messages")
        if not isinstance(messages, list) or not messages:
            fail("E_INPUT", "conversation must contain messages", conv_id=conv_id)
        seen: set[int] = set()
        for message in messages:
            if message.get("conv_id") != conv_id:
                fail(
                    "E_INPUT",
                    "message conv_id must match its conversation",
                    conv_id=conv_id,
                    message_conv_id=message.get("conv_id"),
                )
            msg_no = message.get("msg_no")
            if isinstance(msg_no, bool) or not isinstance(msg_no, int):
                fail("E_INPUT", "msg_no must be an integer", conv_id=conv_id)
            if msg_no in seen:
                fail("E_INPUT", "duplicate msg_no", conv_id=conv_id, msg_no=msg_no)
            seen.add(msg_no)
            for field_name in ("speaker", "text", "locale", "annotator_group"):
                if not isinstance(message.get(field_name), str):
                    fail(
                        "E_INPUT",
                        f"message field {field_name} must be a string",
                        conv_id=conv_id,
                        msg_no=msg_no,
                    )
            weight = message.get("weight", 1.0)
            if (
                isinstance(weight, bool)
                or not isinstance(weight, (int, float))
                or not math.isfinite(float(weight))
                or float(weight) <= 0
            ):
                fail(
                    "E_INPUT",
                    "message weight must be positive",
                    conv_id=conv_id,
                    msg_no=msg_no,
                )
        ordered = sorted(seen)
        if ordered != list(range(1, len(ordered) + 1)):
            fail(
                "E_INPUT",
                "msg_no must be continuous and start at 1",
                conv_id=conv_id,
                msg_nos=ordered,
            )
        messages.sort(key=lambda item: int(item["msg_no"]))
        sale = conversation.get("locale")
        if not isinstance(sale, str) or not sale:
            fail("E_INPUT", "locale must be a non-empty string", conv_id=conv_id)
        group = conversation.get("annotator_group")
        if not isinstance(group, str) or not group:
            fail("E_INPUT", "annotator_group must be a non-empty string", conv_id=conv_id)


def load_conversations(path: Path) -> dict[str, dict[str, Any]]:
    conversations: dict[str, dict[str, Any]] = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                record = json.loads(line)
                conv_id = record.get("conv_id")
                if not isinstance(conv_id, str) or not conv_id:
                    fail("E_INPUT", "conv_id is required", line_no=line_no)
                message = {
                    "conv_id": conv_id,
                    "msg_no": record.get("msg_no"),
                    "speaker": record.get("speaker"),
                    "text": record.get("text"),
                    "locale": record.get("locale"),
                    "annotator_group": record.get("annotator_group"),
                    "weight": float(record.get("weight", 1.0)),
                }
                if conv_id not in conversations:
                    conversations[conv_id] = {
                        "conv_id": conv_id,
                        "locale": message["locale"],
                        "annotator_group": message["annotator_group"],
                        "messages": [],
                    }
                conversations[conv_id]["messages"].append(message)
    except FileNotFoundError:
        fail("E_INPUT", "conversation file does not exist", path=str(path))
    except json.JSONDecodeError as exc:
        fail("E_INPUT", "invalid JSONL", path=str(path), reason=str(exc))
    except OSError as exc:
        fail("E_INPUT", "cannot read conversation file", path=str(path), reason=str(exc))
    validate_conversations(conversations)
    return conversations


def load_inputs(data_dir: Path) -> dict[str, Any]:
    config = _read_json(data_dir / "config.json")
    risk_words = _read_json(data_dir / "risk_words.json")
    conversations = load_conversations(data_dir / "conversations.jsonl")
    validate_config(config)
    validate_risk_words(risk_words)
    configured = set(config["aggregation"]["risk_category_weights"])
    controlled = set(risk_words["categories"])
    if configured != controlled:
        fail(
            "E_CONFIG",
            "risk category weights must exactly match controlled categories",
            configured=sorted(configured),
            controlled=sorted(controlled),
        )
    return {
        "config": config,
        "risk_words": risk_words,
        "conversations": conversations,
    }

