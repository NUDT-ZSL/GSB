from __future__ import annotations

import math
from typing import Any

from .errors import (
    E_COVERAGE_MISMATCH,
    E_TAG_INVALID,
    E_TOKEN_OVERLAP,
    WorkbenchError,
)


def _normalized_distribution(
    weights: dict[str, float], tags: list[str], floor: float = 1e-12
) -> dict[str, float]:
    raw = {
        tag: max(float(weights.get(tag, floor)), floor)
        for tag in tags
    }
    total = sum(raw.values())
    return {tag: raw[tag] / total for tag in tags}


def _transition_distributions(
    transitions: dict[str, dict[str, float]], tags: list[str]
) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for previous in tags:
        row = transitions.get(previous, {})
        result[previous] = _normalized_distribution(row, tags)
    return result


def _token_emissions(
    token: dict[str, Any],
    config: dict[str, Any],
    tags: list[str],
) -> dict[str, float]:
    tagger = config["tagger"]
    default_weight = float(tagger.get("default_emission", 0.01))
    weights = {tag: default_weight for tag in tags}
    hint = token.get("tag_hint")
    if hint in weights:
        if token["source"] == "unknown":
            weights[hint] = float(tagger.get("unknown_emission", 50.0))
        else:
            weights[hint] = float(tagger.get("dictionary_emission", 50.0))

    domain = config["domain"]
    domain_table = config["domain_pos_table"].get(domain, {})
    token_weights = domain_table.get(token["text"], {})
    for tag, weight in token_weights.items():
        if tag in weights and isinstance(weight, (int, float)) and not isinstance(
            weight, bool
        ):
            weights[tag] = max(weights[tag], float(weight))
    return _normalized_distribution(weights, tags)


def _viterbi(
    tags: list[str],
    initial: dict[str, float],
    transitions: dict[str, dict[str, float]],
    emissions: list[dict[str, float]],
) -> list[str]:
    if not emissions:
        return []
    scores = {
        tag: math.log(initial[tag]) + math.log(emissions[0][tag])
        for tag in tags
    }
    backpointers: list[dict[str, str]] = []
    for position in range(1, len(emissions)):
        next_scores: dict[str, float] = {}
        pointers: dict[str, str] = {}
        for tag in tags:
            best_previous = tags[0]
            best_score = (
                scores[best_previous]
                + math.log(transitions[best_previous][tag])
            )
            for previous in tags[1:]:
                candidate_score = (
                    scores[previous] + math.log(transitions[previous][tag])
                )
                if candidate_score > best_score + 1e-12 or (
                    abs(candidate_score - best_score) <= 1e-12
                    and previous < best_previous
                ):
                    best_score = candidate_score
                    best_previous = previous
            next_scores[tag] = best_score + math.log(emissions[position][tag])
            pointers[tag] = best_previous
        scores = next_scores
        backpointers.append(pointers)

    last = tags[0]
    for tag in tags[1:]:
        if scores[tag] > scores[last] + 1e-12 or (
            abs(scores[tag] - scores[last]) <= 1e-12 and tag < last
        ):
            last = tag
    path = [last]
    for pointers in reversed(backpointers):
        last = pointers[last]
        path.append(last)
    path.reverse()
    return path


def _posterior_confidences(
    tags: list[str],
    initial: dict[str, float],
    transitions: dict[str, dict[str, float]],
    emissions: list[dict[str, float]],
    path: list[str],
) -> list[float]:
    if not emissions:
        return []
    alpha: list[dict[str, float]] = []
    first_raw = {
        tag: initial[tag] * emissions[0][tag] for tag in tags
    }
    first_total = sum(first_raw.values())
    alpha.append({tag: first_raw[tag] / first_total for tag in tags})
    for position in range(1, len(emissions)):
        raw = {
            tag: sum(
                alpha[-1][previous]
                * transitions[previous][tag]
                * emissions[position][tag]
                for previous in tags
            )
            for tag in tags
        }
        total = sum(raw.values())
        alpha.append({tag: raw[tag] / total for tag in tags})

    beta: list[dict[str, float]] = [
        {tag: 1.0 for tag in tags} for _ in range(len(emissions))
    ]
    for position in range(len(emissions) - 2, -1, -1):
        raw = {
            tag: sum(
                transitions[tag][next_tag]
                * emissions[position + 1][next_tag]
                * beta[position + 1][next_tag]
                for next_tag in tags
            )
            for tag in tags
        }
        total = sum(raw.values())
        beta[position] = {tag: raw[tag] / total for tag in tags}

    result: list[float] = []
    for position, chosen in enumerate(path):
        raw = {
            tag: alpha[position][tag] * beta[position][tag]
            for tag in tags
        }
        total = sum(raw.values())
        result.append(round(raw[chosen] / total, 8))
    return result


def _validate_tokens(
    sentence_id: str,
    raw_text: str,
    normalized_text: str,
    tokens: list[dict[str, Any]],
) -> None:
    previous_end = 0
    previous_raw_end = 0
    rebuilt: list[str] = []
    for index, token in enumerate(tokens):
        start = token["start"]
        end = token["end"]
        raw_start = token["raw_start"]
        raw_end = token["raw_end"]
        if end <= start or raw_end <= raw_start:
            raise WorkbenchError(
                E_TOKEN_OVERLAP,
                "token has a non-positive span",
                {"sentence_id": sentence_id, "token_index": index},
            )
        if start < previous_end or raw_start < previous_raw_end:
            raise WorkbenchError(
                E_TOKEN_OVERLAP,
                "token spans overlap or are out of order",
                {
                    "sentence_id": sentence_id,
                    "token_index": index,
                    "previous_end": previous_end,
                    "start": start,
                },
            )
        if start != previous_end or raw_start != previous_raw_end:
            raise WorkbenchError(
                E_COVERAGE_MISMATCH,
                "token spans leave a gap",
                {
                    "sentence_id": sentence_id,
                    "token_index": index,
                    "expected_normalized_start": previous_end,
                    "actual_normalized_start": start,
                    "expected_raw_start": previous_raw_end,
                    "actual_raw_start": raw_start,
                },
            )
        expected_text = normalized_text[start:end]
        if token["text"] != expected_text:
            raise WorkbenchError(
                E_COVERAGE_MISMATCH,
                "token text does not match its normalized span",
                {
                    "sentence_id": sentence_id,
                    "token_index": index,
                    "expected": expected_text,
                    "actual": token["text"],
                },
            )
        rebuilt.append(token["text"])
        previous_end = end
        previous_raw_end = raw_end
    if "".join(rebuilt) != normalized_text:
        raise WorkbenchError(
            E_COVERAGE_MISMATCH,
            "token sequence does not cover normalized text",
            {"sentence_id": sentence_id},
        )
    if tokens and previous_raw_end != len(raw_text):
        raise WorkbenchError(
            E_COVERAGE_MISMATCH,
            "token sequence does not cover raw text",
            {
                "sentence_id": sentence_id,
                "expected_raw_end": len(raw_text),
                "actual_raw_end": previous_raw_end,
            },
        )
    if not tokens and raw_text:
        raise WorkbenchError(
            E_COVERAGE_MISMATCH,
            "empty token sequence for non-empty raw text",
            {"sentence_id": sentence_id},
        )


def tag_and_validate(
    normalized: dict[str, Any],
    token_artifact: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    sentence_id = normalized["sentence_id"]
    raw_text = normalized["raw_text"]
    normalized_text = normalized["normalized_text"]
    tokens = token_artifact["tokens"]
    _validate_tokens(sentence_id, raw_text, normalized_text, tokens)

    domain = config["domain"]
    tags = sorted(set(config["domain_tags"][domain]))
    controlled = set(tags)
    tagger = config["tagger"]
    initial = _normalized_distribution(tagger["initial"], tags)
    transitions = _transition_distributions(tagger["transitions"], tags)
    emissions = [_token_emissions(token, config, tags) for token in tokens]
    path = _viterbi(tags, initial, transitions, emissions)
    for index, pos_tag in enumerate(path):
        if pos_tag not in controlled:
            raise WorkbenchError(
                E_TAG_INVALID,
                "POS tag is outside the active domain controlled set",
                {
                    "sentence_id": sentence_id,
                    "token_index": index,
                    "tag": pos_tag,
                    "domain": domain,
                    "controlled_tags": tags,
                },
            )
    confidences = _posterior_confidences(
        tags, initial, transitions, emissions, path
    )

    tagged_tokens: list[dict[str, Any]] = []
    for index, token in enumerate(tokens):
        pos_tag = path[index]
        tagged = dict(token)
        tagged["pos_tag"] = pos_tag
        tagged["confidence"] = confidences[index]
        tagged_tokens.append(tagged)

    raw_slices = [
        raw_text[token["raw_start"] : token["raw_end"]] for token in tagged_tokens
    ]
    return {
        "sentence_id": sentence_id,
        "raw_text": raw_text,
        "normalized_text": normalized_text,
        "tokens": tagged_tokens,
        "pos_sequence": [token["pos_tag"] for token in tagged_tokens],
        "validation": {
            "normalized_coverage": True,
            "raw_coverage": "".join(raw_slices) == raw_text,
            "no_overlap": True,
            "controlled_tags": True,
            "domain": domain,
        },
    }

