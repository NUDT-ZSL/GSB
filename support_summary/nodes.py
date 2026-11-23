"""Pure deterministic implementations of the four DAG nodes."""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

import numpy as np

from .canonical import canonical_bytes, first_difference, sha256_value
from .errors import BusinessError
from .models import (
    WEIGHT_NAMES,
    RewriteConstraints,
    Session,
    SummaryConfig,
    SummaryUnit,
    Vocabulary,
)
from .tokenization import frequency_rows, token_frequencies, tokenize_text, tokenize_turn


PRECISION_SPECS: Dict[str, Tuple[type, int]] = {
    "fp16": (np.float16, 4),
    "fp32": (np.float32, 7),
    "fp64": (np.float64, 12),
}


def quantize(value: float, precision: str) -> float:
    dtype, digits = PRECISION_SPECS[precision]
    rounded = round(float(np.asarray(value, dtype=dtype)), digits)
    if rounded == 0.0:
        return 0.0
    return rounded


def _unit_tokens(
    session: Session, unit: SummaryUnit, vocabulary: Vocabulary
) -> List[str]:
    return tokenize_text(
        unit.text,
        vocabulary,
        session_id=session.session_id,
        turn_index=unit.turn_index,
        field=f"units[{unit.unit_id}].text",
    )


def build_chunks(
    session: Session, vocabulary: Vocabulary, config: SummaryConfig
) -> List[Dict[str, Any]]:
    """Create turn-aligned blocks and vocabulary-bound frequencies."""

    ordered_turns = sorted(session.turns, key=lambda turn: turn.turn_index)
    blocks: List[Dict[str, Any]] = []
    for offset in range(0, len(ordered_turns), config.block_size):
        block_index = offset // config.block_size
        block_turns = ordered_turns[offset : offset + config.block_size]
        tokens = []
        for message_index, turn in enumerate(block_turns):
            tokens.extend(
                tokenize_turn(
                    turn.text,
                    vocabulary,
                    session_id=session.session_id,
                    turn_index=turn.turn_index,
                    message_index=message_index,
                )
            )
        frequencies = frequency_rows(tokens)
        payload = {
            "session_id": session.session_id,
            "vocab_version": vocabulary.vocab_version,
            "precision": config.precision,
            "block_size": config.block_size,
            "block_index": block_index,
            "start_turn_index": block_turns[0].turn_index,
            "end_turn_index": block_turns[-1].turn_index,
            "turn_indices": [turn.turn_index for turn in block_turns],
            "turns": [turn.to_dict() for turn in block_turns],
            "token_count": len(tokens),
            "tokens": [token.to_dict() for token in tokens],
            "frequencies": frequencies,
        }
        blocks.append(payload)
    return blocks


def score_chunks(
    session: Session,
    chunks: Sequence[Mapping[str, Any]],
    vocabulary: Vocabulary,
    config: SummaryConfig,
) -> List[Dict[str, Any]]:
    """Score every truth unit belonging to each chunk."""

    for name in WEIGHT_NAMES:
        if name not in config.weights:
            raise BusinessError(
                "E_WEIGHT_INVALID",
                f"weight {name!r} is missing",
                session_id=session.session_id,
                field=f"weights.{name}",
            )
        if float(config.weights[name]) <= 0.0:
            raise BusinessError(
                "E_WEIGHT_INVALID",
                f"weight {name!r} must be positive",
                session_id=session.session_id,
                field=f"weights.{name}",
                details={"weight": name, "value": config.weights[name]},
            )
    scored_blocks: List[Dict[str, Any]] = []
    for chunk in chunks:
        start = int(chunk["start_turn_index"])
        end = int(chunk["end_turn_index"])
        units = sorted(
            (
                unit
                for unit in session.units
                if start <= unit.turn_index <= end
            ),
            key=lambda unit: (unit.turn_index, unit.unit_index, unit.unit_id),
        )
        entries: List[Dict[str, Any]] = []
        for unit in units:
            words = _unit_tokens(session, unit, vocabulary)
            if not words:
                continue
            counts = Counter(words)
            distinct = len(counts)
            n_tokens = len(words)
            position_raw = 1.0 / (1.0 + unit.turn_index)
            length_raw = n_tokens / (n_tokens + 8.0)
            frequency_raw = (max(counts.values()) + config.smoothing) / (
                n_tokens + config.smoothing
            )
            coverage_raw = distinct / n_tokens
            raw_components = {
                "position": position_raw,
                "length": length_raw,
                "frequency": frequency_raw,
                "coverage": coverage_raw,
            }
            details: Dict[str, Any] = {}
            total = 0.0
            for name in WEIGHT_NAMES:
                component = quantize(raw_components[name], config.precision)
                contribution = quantize(
                    float(config.weights[name]) * component,
                    config.precision,
                )
                total += contribution
                details[name] = {
                    "component": component,
                    "weight": float(config.weights[name]),
                    "contribution": contribution,
                }
            total_score = quantize(total, config.precision)
            entries.append(
                {
                    "unit_id": unit.unit_id,
                    "turn_index": unit.turn_index,
                    "unit_index": unit.unit_index,
                    "text": unit.text,
                    "token_count": n_tokens,
                    "tokens": words,
                    "token_frequencies": token_frequencies(words),
                    "components": details,
                    "total_score": total_score,
                    "selected": True,
                }
            )
        entries.sort(
            key=lambda entry: (
                -float(entry["total_score"]),
                int(entry["turn_index"]),
                int(entry["unit_index"]),
                str(entry["unit_id"]),
            )
        )
        for rank, entry in enumerate(entries):
            entry["rank"] = rank
        payload = {
            "session_id": session.session_id,
            "block_index": int(chunk["block_index"]),
            "start_turn_index": start,
            "end_turn_index": end,
            "chunk_content_hash": sha256_value(dict(chunk)),
            "weights": {name: float(config.weights[name]) for name in WEIGHT_NAMES},
            "smoothing": config.smoothing,
            "precision": config.precision,
            "unit_scores": entries,
        }
        scored_blocks.append(payload)
    return scored_blocks


def _rewrite_tokens(
    words: Sequence[str],
    *,
    strength: float,
    smoothing: float,
    constraints: RewriteConstraints,
    precision: str,
) -> Tuple[List[str], List[Dict[str, Any]], List[str]]:
    forbidden = set(constraints.forbidden_words)
    preserve = set(constraints.preserve_terms)
    counts = Counter(words)
    distinct = max(1, len(counts))
    n_tokens = len(words)
    ranked: List[Tuple[int, str, float, bool]] = []
    for index, word in enumerate(words):
        if word in forbidden:
            continue
        weight = quantize(
            (counts[word] + smoothing) / (n_tokens + smoothing * distinct),
            precision,
        )
        ranked.append((index, word, weight, word in preserve))
    if not ranked:
        for index, word in enumerate(words):
            ranked.append((index, word, 0.0, word in preserve))
        if not ranked:
            return [], [], []
    target = max(1, min(len(ranked), int(math.ceil(n_tokens * (1.0 - strength)))))
    target = min(len(ranked), max(target, sum(1 for item in ranked if item[3])))
    ordered_rank = sorted(
        ranked,
        key=lambda item: (-item[2], 0 if item[3] else 1, item[0]),
    )
    chosen_indices = {item[0] for item in ordered_rank[:target]}
    selected = [
        (index, word, weight, preserved)
        for index, word, weight, preserved in ranked
        if index in chosen_indices
    ]
    selected.sort(key=lambda item: item[0])
    final_words: List[str] = []
    details: List[Dict[str, Any]] = []
    for index, word, weight, preserved in selected:
        candidate = " ".join(final_words + [word])
        if final_words and len(candidate) > constraints.max_chars:
            continue
        final_words.append(word)
        details.append(
            {
                "position": index,
                "word": word,
                "weight": weight,
                "preserve_term": preserved,
            }
        )
    if not final_words and selected:
        final_words = [selected[0][1]]
        details = [
            {
                "position": selected[0][0],
                "word": selected[0][1],
                "weight": selected[0][2],
                "preserve_term": selected[0][3],
            }
        ]
    removed = [
        word
        for index, word in enumerate(words)
        if index not in {detail["position"] for detail in details}
    ]
    return final_words, details, removed


def rewrite_units(
    session: Session,
    scored_blocks: Sequence[Mapping[str, Any]],
    vocabulary: Vocabulary,
    config: SummaryConfig,
) -> List[Dict[str, Any]]:
    """Rewrite each selected unit and group results by chunk block."""

    rewritten_blocks: List[Dict[str, Any]] = []
    units_by_id = {unit.unit_id: unit for unit in session.units}
    for scored_block in sorted(
        scored_blocks, key=lambda block: int(block["block_index"])
    ):
        rewrites: List[Dict[str, Any]] = []
        for entry in scored_block["unit_scores"]:
            if not entry.get("selected", True):
                continue
            unit = units_by_id[str(entry["unit_id"])]
            payload = rewrite_unit_entry(
                session, scored_block, entry, vocabulary, config
            )
            rewrites.append(payload)
        rewrites.sort(
            key=lambda item: (
                int(item["turn_index"]),
                int(item["unit_index"]),
                str(item["unit_id"]),
            )
        )
        rewritten_blocks.append(
            {
                "session_id": session.session_id,
                "block_index": int(scored_block["block_index"]),
                "start_turn_index": int(scored_block["start_turn_index"]),
                "end_turn_index": int(scored_block["end_turn_index"]),
                "rewrites": rewrites,
            }
        )
    return rewritten_blocks


def rewrite_unit_entry(
    session: Session,
    scored_block: Mapping[str, Any],
    entry: Mapping[str, Any],
    vocabulary: Vocabulary,
    config: SummaryConfig,
) -> Dict[str, Any]:
    unit = next(
        item for item in session.units if item.unit_id == str(entry["unit_id"])
    )
    words = _unit_tokens(session, unit, vocabulary)
    final_words, token_details, removed = _rewrite_tokens(
        words,
        strength=config.rewrite_strength,
        smoothing=config.smoothing,
        constraints=session.constraints,
        precision=config.precision,
    )
    return {
        "session_id": session.session_id,
        "block_index": int(scored_block["block_index"]),
        "unit_id": unit.unit_id,
        "turn_index": unit.turn_index,
        "unit_index": unit.unit_index,
        "source_text": unit.text,
        "source_tokens": list(words),
        "rewritten_text": " ".join(final_words),
        "rewritten_tokens": final_words,
        "selected_token_details": token_details,
        "removed_tokens": removed,
        "score": float(entry["total_score"]),
        "score_details": entry["components"],
        "rewrite_strength": config.rewrite_strength,
        "smoothing": config.smoothing,
        "precision": config.precision,
        "constraints_hash": sha256_value(session.constraints.to_dict()),
        "score_content_hash": sha256_value(dict(entry)),
    }


def build_merge_block(
    session_id: str,
    block_index: int,
    rewrites: Sequence[Mapping[str, Any]],
    source_token_count: int,
    source_terms: Iterable[str],
    precision: str,
    start_turn_index: int | None = None,
    end_turn_index: int | None = None,
) -> Dict[str, Any]:
    units = sorted(
        (
            {
                "unit_id": rewrite["unit_id"],
                "turn_index": int(rewrite["turn_index"]),
                "unit_index": int(rewrite["unit_index"]),
                "text": rewrite["rewritten_text"],
                "tokens": list(rewrite["rewritten_tokens"]),
                "score": float(rewrite["score"]),
                "score_details": rewrite["score_details"],
                "rewrite_content_hash": sha256_value(dict(rewrite)),
            }
            for rewrite in rewrites
        ),
        key=lambda unit: (
            int(unit["turn_index"]),
            int(unit["unit_index"]),
            str(unit["unit_id"]),
        ),
    )
    terms = sorted(set(source_terms))
    return {
        "session_id": session_id,
        "block_index": block_index,
        "start_turn_index": start_turn_index,
        "end_turn_index": end_turn_index,
        "units": units,
        "source_token_count": int(source_token_count),
        "source_terms": terms,
        "precision": precision,
    }


def merge_rewrite_blocks(
    session: Session,
    chunk_blocks: Sequence[Mapping[str, Any]],
    rewritten_blocks: Sequence[Mapping[str, Any]],
    config: SummaryConfig,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Create cached block merges and the block-boundary-independent final merge.

    The final result is compared against a one-shot aggregation. Since both
    paths use integer counters and sets, changing block boundaries cannot change
    normalization statistics.
    """

    rewrites_by_block = {
        int(block["block_index"]): block for block in rewritten_blocks
    }
    merge_blocks: List[Dict[str, Any]] = []
    for chunk in sorted(chunk_blocks, key=lambda item: int(item["block_index"])):
        block_index = int(chunk["block_index"])
        rewrite_block = rewrites_by_block.get(block_index, {"rewrites": []})
        merge_blocks.append(
            build_merge_block(
                session.session_id,
                block_index,
                rewrite_block["rewrites"],
                int(chunk["token_count"]),
                (str(row["word"]) for row in chunk["frequencies"]),
                config.precision,
                int(chunk["start_turn_index"]),
                int(chunk["end_turn_index"]),
            )
        )

    final = finalize_merge_blocks(
        session.session_id, merge_blocks, config.precision
    )
    all_source_terms = sorted(
        {term for block in merge_blocks for term in block["source_terms"]}
    )
    all_rewrites = [
        rewrite
        for block in sorted(rewritten_blocks, key=lambda item: int(item["block_index"]))
        for rewrite in block["rewrites"]
    ]
    one_shot_block = build_merge_block(
        session.session_id,
        0,
        all_rewrites,
        sum(int(block["source_token_count"]) for block in merge_blocks),
        all_source_terms,
        config.precision,
    )
    one_shot = finalize_merge_blocks(
        session.session_id, [one_shot_block], config.precision
    )
    left = canonical_bytes(
        {
            "summary_text": final["summary_text"],
            "units": final["units"],
            "quality": final["quality"],
        }
    )
    right = canonical_bytes(
        {
            "summary_text": one_shot["summary_text"],
            "units": one_shot["units"],
            "quality": one_shot["quality"],
        }
    )
    if left != right:
        left_text = left.decode("utf-8")
        right_text = right.decode("utf-8")
        difference = first_difference(left_text, right_text) or {"position": 0}
        raise BusinessError(
            "E_BLOCK_MISMATCH",
            "block merge differs from one-shot merge",
            session_id=session.session_id,
            field="summary_merge",
            details={
                "first_difference_position": difference["position"],
                "left": difference.get("left", ""),
                "right": difference.get("right", ""),
            },
        )
    return merge_blocks, final


def finalize_merge_blocks(
    session_id: str,
    merge_blocks: Sequence[Mapping[str, Any]],
    precision: str,
) -> Dict[str, Any]:
    units = [unit for block in merge_blocks for unit in block["units"]]
    units.sort(
        key=lambda unit: (
            int(unit["turn_index"]),
            int(unit["unit_index"]),
            str(unit["unit_id"]),
        )
    )
    summary_tokens = [token for unit in units for token in unit["tokens"]]
    summary_terms = sorted(set(summary_tokens))
    source_terms = sorted(
        {term for block in merge_blocks for term in block["source_terms"]}
    )
    source_token_count = sum(
        int(block["source_token_count"]) for block in merge_blocks
    )
    summary_token_count = len(summary_tokens)
    compression_ratio = quantize(
        summary_token_count / source_token_count if source_token_count else 0.0,
        precision,
    )
    coverage_ratio = quantize(
        len(summary_terms) / len(source_terms) if source_terms else 0.0,
        precision,
    )
    average_score = quantize(
        sum(float(unit["score"]) for unit in units) / len(units)
        if units
        else 0.0,
        precision,
    )
    quality_score = quantize(
        max(
            0.0,
            min(
                1.0,
                (coverage_ratio + (1.0 - min(compression_ratio, 1.0))) / 2.0,
            ),
        ),
        precision,
    )
    trace = []
    cumulative_units = 0
    cumulative_tokens = 0
    for block in sorted(merge_blocks, key=lambda item: int(item["block_index"])):
        cumulative_units += len(block["units"])
        cumulative_tokens += len(
            [token for unit in block["units"] for token in unit["tokens"]]
        )
        trace.append(
            {
                "block_index": int(block["block_index"]),
                "unit_count": len(block["units"]),
                "block_summary_token_count": len(
                    [token for unit in block["units"] for token in unit["tokens"]]
                ),
                "cumulative_unit_count": cumulative_units,
                "cumulative_summary_token_count": cumulative_tokens,
            }
        )
    return {
        "session_id": session_id,
        "summary_text": " ".join(unit["text"] for unit in units if unit["text"]),
        "units": units,
        "quality": {
            "unit_count": len(units),
            "summary_token_count": summary_token_count,
            "source_token_count": source_token_count,
            "compression_ratio": compression_ratio,
            "coverage_ratio": coverage_ratio,
            "average_unit_score": average_score,
            "quality_score": quality_score,
            "unique_summary_terms": summary_terms,
            "unique_source_terms": source_terms,
        },
        "block_trace": trace,
        "block_count": len(merge_blocks),
        "precision": precision,
    }


def verify_one_shot_equivalence(
    session: Session,
    merge_blocks: Sequence[Mapping[str, Any]],
    rewritten_blocks: Sequence[Mapping[str, Any]],
    precision: str,
) -> None:
    """Assert that block-wise and one-shot aggregation are byte-identical."""

    final = finalize_merge_blocks(session.session_id, merge_blocks, precision)
    all_source_terms = sorted(
        {term for block in merge_blocks for term in block["source_terms"]}
    )
    all_rewrites = [
        rewrite
        for block in sorted(rewritten_blocks, key=lambda item: int(item["block_index"]))
        for rewrite in block["rewrites"]
    ]
    one_shot_block = build_merge_block(
        session.session_id,
        0,
        all_rewrites,
        sum(int(block["source_token_count"]) for block in merge_blocks),
        all_source_terms,
        precision,
    )
    one_shot = finalize_merge_blocks(session.session_id, [one_shot_block], precision)
    left = canonical_bytes(
        {
            "summary_text": final["summary_text"],
            "units": final["units"],
            "quality": final["quality"],
        }
    )
    right = canonical_bytes(
        {
            "summary_text": one_shot["summary_text"],
            "units": one_shot["units"],
            "quality": one_shot["quality"],
        }
    )
    if left != right:
        difference = first_difference(
            left.decode("utf-8"), right.decode("utf-8")
        ) or {"position": 0}
        raise BusinessError(
            "E_BLOCK_MISMATCH",
            "block merge differs from one-shot merge",
            session_id=session.session_id,
            field="summary_merge",
            details={
                "first_difference_position": difference["position"],
                "left": difference.get("left", ""),
                "right": difference.get("right", ""),
            },
        )


def recompute_quality_from_details(
    details: Mapping[str, Any], precision: str
) -> Dict[str, Any]:
    """Independently recompute final aggregate fields from stored unit details."""

    units = list(details["units"])
    summary_tokens = [token for unit in units for token in unit["tokens"]]
    source_terms = list(details["quality"]["unique_source_terms"])
    source_token_count = int(details["quality"]["source_token_count"])
    summary_terms = sorted(set(summary_tokens))
    compression_ratio = quantize(
        len(summary_tokens) / source_token_count if source_token_count else 0.0,
        precision,
    )
    coverage_ratio = quantize(
        len(summary_terms) / len(source_terms) if source_terms else 0.0,
        precision,
    )
    average_score = quantize(
        sum(float(unit["score"]) for unit in units) / len(units) if units else 0.0,
        precision,
    )
    quality_score = quantize(
        max(
            0.0,
            min(
                1.0,
                (coverage_ratio + (1.0 - min(compression_ratio, 1.0))) / 2.0,
            ),
        ),
        precision,
    )
    return {
        "summary_text": " ".join(unit["text"] for unit in units if unit["text"]),
        "unit_count": len(units),
        "summary_token_count": len(summary_tokens),
        "compression_ratio": compression_ratio,
        "coverage_ratio": coverage_ratio,
        "average_unit_score": average_score,
        "quality_score": quality_score,
        "unique_summary_terms": summary_terms,
    }
