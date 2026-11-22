"""Deterministic local sample and configuration generator."""

from __future__ import annotations

import argparse
import random
from pathlib import Path
from typing import Any

from .canonical import atomic_write_bytes, atomic_write_json, canonical_bytes
from .config import validate_config


GROUPS = ("G1", "G2", "G3", "G4")
TOPICS = (
    "safe summarization",
    "reward-model calibration",
    "preference annotation",
    "instruction following",
    "toxicity avoidance",
    "helpfulness ranking",
    "citation faithfulness",
    "refusal quality",
    "dialogue coherence",
    "data privacy",
)
WORDS = (
    "clear",
    "concise",
    "grounded",
    "careful",
    "safe",
    "useful",
    "direct",
    "factual",
    "complete",
    "respectful",
    "specific",
    "relevant",
    "balanced",
    "verified",
    "practical",
    "measured",
    "precise",
    "readable",
    "stable",
    "scoped",
)
NOISE = ("\x00", "\x07", "\x1f", "\u200b", "\ue000", "\ufffd")


def default_config() -> dict[str, Any]:
    quotas = []
    targets = {
        ("G1", "A"): 7,
        ("G1", "B"): 6,
        ("G1", "C"): 4,
        ("G2", "A"): 7,
        ("G2", "B"): 6,
        ("G2", "C"): 4,
        ("G3", "A"): 6,
        ("G3", "B"): 5,
        ("G3", "C"): 3,
        ("G4", "A"): 5,
        ("G4", "B"): 4,
        ("G4", "C"): 20,
    }
    for group in GROUPS:
        for tier in ("A", "B", "C"):
            quotas.append(
                {
                    "annotator_group": group,
                    "quality_tier": tier,
                    "target": targets[(group, tier)],
                }
            )
    return {
        "schema_version": "1.0",
        "version": 1,
        "seed": 20261123,
        "quality": {
            "weights": {"length": 0.30, "repetition": 0.35, "noise": 0.35},
            "target_chars": 220,
            "pass_threshold": 0.60,
            "tiers": [
                {"name": "A", "min_score": 0.93},
                {"name": "B", "min_score": 0.75},
                {"name": "C", "min_score": 0.0},
            ],
        },
        "neardup": {
            "chunk_tokens": 3,
            "threshold": 0.62,
            "threshold_mode": "tolerance",
            "tolerance": 0.02,
        },
        "consistency": {"normalization": "nfkc_casefold_ws", "smoothing": 0.5},
        "sampling": {"quotas": quotas, "max_total": None},
        "scheduler": {
            "max_workers": 4,
            "max_batch_pairs": 16,
            "mac_budget": 240000,
            "memory_budget_bytes": 2097152,
            "cost_model": {
                "mac_per_pair": 1200,
                "mac_per_comparison": 9,
                "mac_reduction_per_batch": 900,
                "mac_worker_overhead": 1500,
                "memory_fixed_bytes": 1048576,
                "memory_per_worker_bytes": 262144,
                "memory_per_pair_bytes": 4096,
                "memory_per_comparison_bytes": 64,
            },
        },
        "precision": "fp64",
        "priority_order": [
            "progress",
            "memory",
            "grouping_quality",
            "parallelism",
            "wall_time",
            "batch_efficiency",
        ],
    }


def _profile_text(rng: random.Random, topic: str, profile: str, variant: int) -> str:
    base = [f"Topic_{topic.replace(' ', '_')}", f"variant_{variant}"]
    if profile == "clean":
        tokens = base + [
            f"detail_{variant}_{index}_{rng.choice(WORDS)}" for index in range(28)
        ]
        text = " ".join(tokens) + "."
    elif profile == "borderline":
        phrase = [rng.choice(WORDS) for _ in range(8)]
        tokens = base + phrase * 3 + [rng.choice(WORDS) for _ in range(10)]
        text = " ".join(tokens) + " ."
        text += " " + "".join(rng.choice(NOISE) for _ in range(4))
    else:
        phrase = [rng.choice(WORDS) for _ in range(6)]
        tokens = base + phrase * 5
        text = " ".join(tokens)
        insert_at = max(1, len(text) // 3)
        noise = "".join(rng.choice(NOISE) for _ in range(58))
        text = text[:insert_at] + noise + text[insert_at:]
        text += " " + " ".join(rng.choice(NOISE) for _ in range(12))
    return text


def _pair_profile(index: int) -> tuple[str, str, str]:
    bucket = index % 9
    if bucket in (0, 1, 2, 3):
        return "clean", "clean", "clean"
    if bucket in (4, 5):
        return "borderline", "borderline", "borderline"
    if bucket == 6:
        return "clean", "borderline", "clean"
    if bucket == 7:
        return "borderline", "noisy", "borderline"
    return "noisy", "noisy", "noisy"


def generate_pairs(count: int = 120, seed: int = 20261123) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    pairs: list[dict[str, Any]] = []
    for index in range(count):
        chosen_profile, rejected_profile, flag = _pair_profile(index)
        topic = TOPICS[index % len(TOPICS)]
        prompt = f"Compare two responses about {topic}. Case {index:04d}."
        chosen = _profile_text(rng, topic, chosen_profile, index * 2 + 1)
        rejected = _profile_text(rng, topic, rejected_profile, index * 2 + 2)
        pairs.append(
            {
                "pair_id": f"P{index:05d}",
                "prompt": prompt,
                "chosen": chosen,
                "rejected": rejected,
                "annotator_group": GROUPS[index % len(GROUPS)],
                "quality_flag": flag,
            }
        )

    # Near duplicates: copy a stable predecessor and mutate one token.
    for index in range(18, count, 13):
        source = pairs[index - 7]
        chosen = source["chosen"].replace("clear", "clearly", 1)
        rejected = source["rejected"].replace("clear", "clearly", 1)
        pairs[index].update(
            {
                "prompt": source["prompt"].replace("Case", "Near-duplicate case"),
                "chosen": chosen,
                "rejected": rejected,
                "annotator_group": source["annotator_group"],
                "quality_flag": source["quality_flag"],
            }
        )

    _inject_conflicts(pairs)
    pairs.sort(key=lambda item: item["pair_id"])
    return pairs


def _inject_conflicts(pairs: list[dict[str, Any]]) -> None:
    if len(pairs) < 14:
        return
    # Same group, same prompt, reversed sides: group-direction conflict.
    prompt = "Conflict probe: same annotator group."
    chosen = _profile_text(random.Random(991), "same-group conflict", "clean", 1)
    rejected = _profile_text(random.Random(992), "same-group conflict", "clean", 2)
    pairs[3].update(
        {
            "prompt": prompt,
            "chosen": chosen,
            "rejected": rejected,
            "annotator_group": "G1",
            "quality_flag": "clean",
        }
    )
    pairs[4].update(
        {
            "prompt": prompt,
            "chosen": rejected,
            "rejected": chosen,
            "annotator_group": "G1",
            "quality_flag": "clean",
        }
    )

    # Different groups, same prompt, reversed sides: cross contradiction only.
    prompt = "Conflict probe: cross annotator groups."
    chosen = _profile_text(random.Random(993), "cross-group conflict", "borderline", 3)
    rejected = _profile_text(random.Random(994), "cross-group conflict", "borderline", 4)
    pairs[7].update(
        {
            "prompt": prompt,
            "chosen": chosen,
            "rejected": rejected,
            "annotator_group": "G2",
            "quality_flag": "borderline",
        }
    )
    pairs[8].update(
        {
            "prompt": prompt,
            "chosen": rejected,
            "rejected": chosen,
            "annotator_group": "G3",
            "quality_flag": "borderline",
        }
    )

    # Identical sides: independently detectable contradiction.
    same = _profile_text(random.Random(995), "identical sides", "clean", 5)
    pairs[11].update(
        {
            "prompt": "Conflict probe: identical sides.",
            "chosen": same,
            "rejected": same,
            "annotator_group": "G4",
            "quality_flag": "clean",
        }
    )


def write_sample(
    out_path: str | Path,
    config_path: str | Path,
    *,
    count: int = 120,
    seed: int = 20261123,
) -> dict[str, Any]:
    pairs = generate_pairs(count=count, seed=seed)
    data = b"".join(canonical_bytes(pair) + b"\n" for pair in pairs)
    atomic_write_bytes(out_path, data)
    config = default_config()
    config["seed"] = seed
    validate_config(config)
    atomic_write_json(config_path, config)
    return {
        "out": str(Path(out_path)),
        "config": str(Path(config_path)),
        "pairs": len(pairs),
        "seed": seed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/sample.jsonl")
    parser.add_argument("--config", default="configs/default.json")
    parser.add_argument("--count", type=int, default=120)
    parser.add_argument("--seed", type=int, default=20261123)
    args = parser.parse_args(argv)
    result = write_sample(args.out, args.config, count=args.count, seed=args.seed)
    import json

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
