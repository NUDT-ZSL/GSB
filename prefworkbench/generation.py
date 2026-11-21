"""Deterministic local preference-pair and configuration generation."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from .canonical import canonical_json, file_hash, write_json, write_jsonl
from .config import default_config, write_config
from .signatures import signature_params
from .textutil import NOISE_CHARS


TOPICS = (
    "a city library",
    "distributed tracing",
    "sourdough baking",
    "battery thermal design",
    "a neighborhood garden",
    "open-source maintenance",
    "coastal weather",
    "database indexing",
    "language learning",
    "public transport",
    "solar panel cleaning",
    "technical writing",
    "small team planning",
    "noise-cancelling headphones",
    "river restoration",
    "local caching",
    "data annotation",
    "reward model evaluation",
    "reproducible experiments",
    "accessibility testing",
    "supply chain risk",
    "search ranking",
    "sensor calibration",
    "privacy review",
    "meeting facilitation",
    "release rollback",
    "backup verification",
    "offline documentation",
)

PROMPTS = (
    "Explain the main tradeoffs in {topic} for a small engineering team.",
    "Write a concise checklist for {topic} and mention one common failure.",
    "Compare two practical approaches to {topic}; keep the answer concrete.",
    "Describe how you would validate a decision about {topic}.",
)

CHOSEN = (
    "Start with the smallest measurement that can falsify the assumption about {topic}, "
    "then document the result, rollback condition, and one owner for follow-up.",
    "Use a short experiment around {topic}, keep the comparison baseline identical, "
    "and record the failure mode before scaling the approach.",
    "Define observable acceptance criteria for {topic}, test the boundary cases, "
    "and preserve evidence so another teammate can reproduce the decision.",
)

REJECTED = (
    "Adopt the most complex option immediately and ignore {topic} until a problem appears.",
    "Rely on intuition for {topic}; documenting the baseline would only slow the team down.",
    "Change several aspects of {topic} at once so the result arrives sooner.",
)

GROUPS = ("g-alpha", "g-beta", "g-gamma")
WORDS = (
    "calibration",
    "baseline",
    "latency",
    "quality",
    "budget",
    "coverage",
    "boundary",
    "replay",
    "signal",
    "variance",
    "cache",
    "quota",
    "trace",
    "tolerance",
)


def _noise(rng: random.Random, text: str, count: int) -> str:
    chars = list(text)
    if not chars:
        return text
    for _ in range(count):
        index = rng.randrange(len(chars))
        insertion = rng.choice(tuple(sorted(NOISE_CHARS)))
        chars.insert(index, insertion)
    return "".join(chars)


def _base_sample(rng: random.Random, index: int) -> dict[str, str]:
    topic = TOPICS[index % len(TOPICS)]
    prompt = PROMPTS[index % len(PROMPTS)].format(topic=topic)
    chosen = CHOSEN[index % len(CHOSEN)].format(topic=topic)
    rejected = REJECTED[index % len(REJECTED)].format(topic=topic)
    filler = " ".join(rng.choice(WORDS) for _ in range(12))
    chosen = f"{chosen} Evidence tags: {filler}."
    rejected = f"{rejected} Alternative tags: {filler}."
    return {
        "prompt": prompt,
        "chosen": chosen,
        "rejected": rejected,
    }


def generate_samples(seed: int = 20261122, count: int = 42) -> list[dict[str, Any]]:
    """Generate stable preference pairs, including duplicates and low-quality cases."""

    if count < 1:
        raise ValueError("sample count must be positive")
    rng = random.Random(seed)
    roots = [_base_sample(rng, index) for index in range(max(1, count))]
    rows: list[dict[str, Any]] = []
    duplicate_sources = [7, 13, 19, 25, 31, 37]
    near_sources = [4, 10, 16, 22, 28, 34]

    for index in range(count):
        if index in duplicate_sources:
            source = roots[(index * 5 + 3) % len(roots)]
            prompt = source["prompt"]
            chosen = source["chosen"]
            rejected = source["rejected"]
            note = "synthetic:exact-duplicate"
        elif index in near_sources:
            source = roots[(index * 3 + 7) % len(roots)]
            prompt = source["prompt"].replace("small", "compact")
            chosen = source["chosen"].replace("smallest", "leanest").replace("same", "matching")
            rejected = source["rejected"]
            note = "synthetic:near-duplicate"
        else:
            source = roots[index]
            prompt = source["prompt"]
            chosen = source["chosen"]
            rejected = source["rejected"]
            note = "synthetic:base"

        if index in (11, 23):
            repeated = "quality quality contract quality " * 58
            noise_tail = "".join(
                rng.choice(tuple(sorted(NOISE_CHARS)))
                for _ in range(1000 if index == 23 else 420)
            )
            chosen = repeated + noise_tail + chosen
            rejected = repeated + noise_tail[: len(noise_tail) // 2] + rejected
            prompt = _noise(rng, prompt, 10)
            note = "synthetic:low-quality-repetition-noise"
        elif index in (5, 20, 35):
            repeated = "baseline contract quality review " * 18
            chosen = repeated + chosen
            rejected = repeated[: len(repeated) // 2] + rejected
            prompt = _noise(rng, prompt, 4)
            note = "synthetic:medium-quality-repetition"
        elif index % 7 == 0:
            chosen = _noise(rng, chosen, 4)
            note += "+light-noise"

        rows.append(
            {
                "pair_id": f"pair-{index + 1:04d}",
                "prompt": prompt,
                "chosen": chosen,
                "rejected": rejected,
                "annotator_group": GROUPS[index % len(GROUPS)],
                "quality_notes": note,
            }
        )
    return rows


def write_dataset(
    root: str | Path,
    seed: int = 20261122,
    count: int = 42,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = Path(root)
    target.mkdir(parents=True, exist_ok=True)
    effective_config = config or default_config(seed)
    samples = generate_samples(seed, count)
    samples_path = target / "preference_pairs.jsonl"
    config_path = target / "construction_config.json"
    signature_path = target / "signature_params.json"
    write_jsonl(samples_path, samples)
    write_config(config_path, effective_config)
    write_json(signature_path, signature_params(effective_config["near_duplicate"]))
    return {
        "samples_path": str(samples_path),
        "config_path": str(config_path),
        "signature_params_path": str(signature_path),
        "sample_count": len(samples),
        "samples_sha256": file_hash(samples_path),
        "config_sha256": file_hash(config_path),
        "first_sample": samples[0],
        "last_sample": samples[-1],
        "canonical_sample_bytes": len(canonical_json(samples[0]).encode("utf-8")),
    }


def write_default_config(path: str | Path, seed: int = 20261122) -> None:
    write_config(path, default_config(seed))

