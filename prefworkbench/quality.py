"""Deterministic weighted quality scoring and batched process-pool execution."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction
from typing import Any

from .errors import E_WORKER_FAILURE


TOKEN_RE = re.compile(r"\w+", re.UNICODE)
ALLOWED_PUNCTUATION = set(".,!?;:'\"()-[]{}/@#$%&*+=<>|_~`\\")


def tokenize(text: str) -> list[str]:
    return [match.group(0).casefold() for match in TOKEN_RE.finditer(text)]


def is_noise_char(char: str) -> bool:
    if char.isspace():
        return False
    category = unicodedata.category(char)
    if category[0] == "C":
        return True
    if char.isalnum() or char in ALLOWED_PUNCTUATION:
        return False
    return category[0] in ("S", "Z")


def _fraction(value: Any) -> Fraction:
    return Fraction(str(value))


def _round_fraction(value: Fraction, decimals: int) -> float:
    with localcontext() as context:
        context.prec = 60
        decimal_value = Decimal(value.numerator) / Decimal(value.denominator)
        quantum = Decimal(1).scaleb(-decimals)
        rounded = decimal_value.quantize(quantum, rounding=ROUND_HALF_UP)
    result = float(rounded)
    return 0.0 if result == 0.0 else result


def score_response(text: str, quality_config: dict[str, Any], decimals: int) -> dict[str, Any]:
    tokens = tokenize(text)
    token_count = len(tokens)
    unique_count = len(set(tokens))
    repeated = token_count - unique_count
    length_chars = len(text)
    target_chars = int(quality_config["target_chars"])
    noise_count = sum(1 for char in text if is_noise_char(char))

    length_fraction = min(Fraction(1, 1), Fraction(length_chars, target_chars))
    repetition_fraction = (
        Fraction(repeated, token_count) if token_count else Fraction(0, 1)
    )
    noise_fraction = (
        Fraction(noise_count, length_chars) if length_chars else Fraction(0, 1)
    )
    raw_scores = {
        "length": length_fraction,
        "repetition": Fraction(1, 1) - repetition_fraction,
        "noise": Fraction(1, 1) - noise_fraction,
    }
    weights = {
        key: _fraction(quality_config["weights"][key])
        for key in ("length", "repetition", "noise")
    }
    components: dict[str, Any] = {}
    total = Fraction(0, 1)
    for key in ("length", "repetition", "noise"):
        contribution = weights[key] * raw_scores[key]
        total += contribution
        components[key] = {
            "weight": _round_fraction(weights[key], decimals),
            "raw_score": _round_fraction(raw_scores[key], decimals),
            "contribution": _round_fraction(contribution, decimals),
        }

    return {
        "length_chars": length_chars,
        "token_count": token_count,
        "unique_token_count": unique_count,
        "repetition_rate": _round_fraction(repetition_fraction, decimals),
        "noise_char_count": noise_count,
        "noise_char_ratio": _round_fraction(noise_fraction, decimals),
        "components": components,
        "score": _round_fraction(total, decimals),
    }


def score_pair(
    pair: dict[str, Any],
    quality_config: dict[str, Any],
    *,
    decimals: int = 8,
) -> dict[str, Any]:
    chosen = score_response(str(pair["chosen"]), quality_config, decimals)
    rejected = score_response(str(pair["rejected"]), quality_config, decimals)
    pair_total = (
        _fraction(chosen["score"]) + _fraction(rejected["score"])
    ) / 2
    pair_score = _round_fraction(pair_total, decimals)
    tier_name = quality_config["tiers"][-1]["name"]
    for tier in quality_config["tiers"]:
        if pair_score >= float(tier["min_score"]):
            tier_name = str(tier["name"])
            break
    passed = pair_score >= float(quality_config["pass_threshold"])
    return {
        "pair_id": str(pair["pair_id"]),
        "chosen": chosen,
        "rejected": rejected,
        "pair_score": pair_score,
        "quality_tier": tier_name,
        "pass": passed,
    }


def quality_worker(payload: dict[str, Any]) -> dict[str, Any]:
    """Top-level process-pool entry point; all failures stay structured."""
    batch_id = int(payload["batch_id"])
    quality_config = payload["quality_config"]
    decimals = int(payload["decimals"])
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for pair in payload["pairs"]:
        pair_id = str(pair["pair_id"])
        try:
            if pair.get("__fail_stage__") == "quality":
                raise RuntimeError("injected quality failure")
            records.append(score_pair(pair, quality_config, decimals=decimals))
        except Exception as exc:  # noqa: BLE001 - process boundary must report every pair
            errors.append(
                {
                    "code": E_WORKER_FAILURE,
                    "stage": "quality",
                    "pair_ids": [pair_id],
                    "batch_id": batch_id,
                    "message": str(exc),
                    "exception_type": type(exc).__name__,
                }
            )
    return {
        "batch_id": batch_id,
        "records": records,
        "errors": errors,
    }
