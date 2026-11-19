from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import canonical_bytes, hash_value, readable_json_bytes
from .config import DEFAULT_CONFIG, validate_config
from .errors import E_INPUT, WorkbenchError


PROMPT_BASES = [
    "Explain why a fixed random seed matters in a reproducible evaluation pipeline.",
    "Compare precision and recall for an imbalanced reward-model validation set.",
    "Describe how to detect annotation drift without using wall-clock timestamps.",
    "Give a deterministic procedure for deduplicating preference-pair training data.",
    "Explain the risk of leaking a rejected response into the chosen training split.",
    "Summarize how cache keys should include upstream artifact hashes.",
    "Describe a fair way to allocate a fixed reward-model sampling budget.",
    "Explain how quality tiers can interact with annotator-group quotas.",
    "Outline an audit trail for every sample in a released preference dataset.",
    "Compare exact prompt grouping with fuzzy prompt grouping for consistency checks.",
]

PROMPT_VARIANTS = [
    "",
    " Answer in two short paragraphs.",
    " Use one concrete example.",
    " Keep the response concise.",
    " Mention the main failure mode.",
]

CHOSEN_DETAILS = [
    "The fixed seed makes every shuffle, split, and tie-break reproducible.",
    "A stable seed prevents a rerun from silently changing the selected examples.",
    "Seeded hashing gives deterministic ranking while avoiding process-global state.",
    "The seed is persisted next to the manifest so another operator can replay it.",
    "All random choices are derived from the declared seed and pair identifiers.",
]

REJECTED_DETAILS = [
    "Relying on ambient entropy makes comparisons between runs unreliable.",
    "A timestamp-based order can move a sample between train and validation.",
    "An undeclared random shuffle breaks byte-level manifest comparisons.",
    "Reading mutable global state makes the same request produce different data.",
    "Unrecorded sampling choices prevent a reviewer from reproducing the quota.",
]


def _make_sample(
    index: int,
    *,
    seed: int,
    groups: list[str],
    rng: np.random.Generator,
) -> dict[str, Any]:
    family = index % len(PROMPT_BASES)
    variant = (index // len(PROMPT_BASES)) % len(PROMPT_VARIANTS)
    prompt = PROMPT_BASES[family] + PROMPT_VARIANTS[variant]
    group = groups[int(rng.integers(0, len(groups)))]
    flag = "ok"
    if index % 11 == 5:
        flag = "bad"
    elif index % 7 == 3:
        flag = "warn"

    chosen = (
        f"Chosen answer {index:04d}: "
        f"{CHOSEN_DETAILS[(index + family) % len(CHOSEN_DETAILS)]} "
        f"The recommendation is deterministic and auditable for case {family}."
    )
    rejected = (
        f"Rejected answer {index:04d}: "
        f"{REJECTED_DETAILS[(index + 2 * family) % len(REJECTED_DETAILS)]} "
        f"It leaves case {family} dependent on unstable external state."
    )

    if index % 13 == 4:
        rejected = "too short"
    if index % 17 == 6:
        chosen = "repeat repeat repeat repeat repeat repeat"
    if index % 19 == 7:
        rejected = "Noise ### $$$ %%% ??? " + rejected

    return {
        "pair_id": f"p{index + 1:04d}",
        "prompt": prompt,
        "chosen": chosen,
        "rejected": rejected,
        "annotator_group": group,
        "annotation_version": 1,
        "quality_flag": flag,
        "generation_seed": seed,
        "generation_index": index,
    }


def _conflict_samples(seed: int) -> list[dict[str, Any]]:
    prompt = (
        "A shared response must never be treated as both preferred and dispreferred "
        "under the same prompt."
    )
    shared = (
        "This exact response is preferred in one annotation and rejected in another, "
        "so the pair conflict must be recorded explicitly."
    )
    return [
        {
            "pair_id": "conflict_a",
            "prompt": prompt,
            "chosen": shared,
            "rejected": "This response is only dispreferred in the first pair.",
            "annotator_group": "alpha",
            "annotation_version": 1,
            "quality_flag": "ok",
            "generation_seed": seed,
            "generation_index": 1000,
        },
        {
            "pair_id": "conflict_b",
            "prompt": prompt,
            "chosen": "This response is only preferred in the second pair.",
            "rejected": shared,
            "annotator_group": "beta",
            "annotation_version": 1,
            "quality_flag": "ok",
            "generation_seed": seed,
            "generation_index": 1001,
        },
    ]


def generate_samples(
    *,
    seed: int,
    count: int = 36,
    groups: list[str] | None = None,
    with_conflict: bool = False,
) -> list[dict[str, Any]]:
    if count < 1:
        raise WorkbenchError(E_INPUT, "sample count must be positive")
    if seed < 0:
        raise WorkbenchError(E_INPUT, "seed must be non-negative")
    group_names = groups or ["alpha", "beta", "gamma"]
    if not group_names or len(set(group_names)) != len(group_names):
        raise WorkbenchError(E_INPUT, "annotator groups must be non-empty and unique")

    rng = np.random.default_rng(seed)
    samples = [
        _make_sample(index, seed=seed, groups=group_names, rng=rng)
        for index in range(count)
    ]
    if with_conflict:
        samples.extend(_conflict_samples(seed))
    samples.sort(key=lambda item: item["pair_id"])
    return samples


def generate_config(
    *,
    seed: int = 20261120,
    groups: list[str] | None = None,
    count: int = 36,
    conflict_policy: str = "error",
) -> dict[str, Any]:
    group_names = groups or ["alpha", "beta", "gamma"]
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["generation"] = {
        "seed": seed,
        "sample_count": count,
        "annotator_groups": list(group_names),
    }
    config["near_duplicate"]["seed"] = seed + 7001
    config["sampling"]["seed"] = seed + 31007
    config["sampling"]["total_target"] = max(6, (count * 2) // 3)
    base = config["sampling"]["total_target"] // len(group_names)
    remainder = config["sampling"]["total_target"] - base * len(group_names)
    config["sampling"]["per_annotator_group"] = {
        name: base + (1 if index < remainder else 0)
        for index, name in enumerate(group_names)
    }
    high = max(1, config["sampling"]["total_target"] // 2)
    medium = max(1, config["sampling"]["total_target"] // 3)
    low = max(0, config["sampling"]["total_target"] - high - medium)
    config["sampling"]["per_quality_tier"] = {
        "high": high,
        "medium": medium,
        "low": low,
    }
    config["pair_consistency"]["conflict_policy"] = conflict_policy
    return validate_config(config)


def write_jsonl(path: Path | str, records: list[dict[str, Any]]) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [canonical_bytes(record) for record in sorted(records, key=lambda item: item["pair_id"])]
    data = b"\n".join(lines) + (b"\n" if lines else b"")
    target.write_bytes(data)
    return hash_value(sorted(records, key=lambda item: item["pair_id"]))


def write_config(path: Path | str, config: dict[str, Any]) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized = validate_config(config)
    target.write_bytes(readable_json_bytes(normalized))
    return hash_value(normalized)


def generate_workspace(
    output_dir: Path | str,
    *,
    seed: int = 20261120,
    count: int = 36,
    groups: list[str] | None = None,
    with_conflict: bool = False,
    conflict_policy: str = "error",
) -> dict[str, Any]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    samples = generate_samples(seed=seed, count=count, groups=groups, with_conflict=with_conflict)
    config = generate_config(
        seed=seed,
        groups=groups,
        count=len(samples),
        conflict_policy=conflict_policy,
    )
    sample_path = root / "samples.jsonl"
    config_path = root / "config.json"
    sample_hash = write_jsonl(sample_path, samples)
    config_hash = write_config(config_path, config)
    return {
        "samples_path": str(sample_path),
        "config_path": str(config_path),
        "samples_sha256": sample_hash,
        "config_sha256": config_hash,
        "sample_count": len(samples),
        "seed": seed,
    }


def read_samples(path: Path | str) -> tuple[list[dict[str, Any]], str]:
    source = Path(path)
    records: list[dict[str, Any]] = []
    try:
        with source.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise WorkbenchError(
                        E_INPUT,
                        "invalid sample JSON",
                        {"line": line_number, "error": str(exc)},
                    ) from exc
                records.append(record)
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkbenchError(E_INPUT, "cannot read samples", {"path": str(source), "error": str(exc)})

    required = {
        "pair_id",
        "prompt",
        "chosen",
        "rejected",
        "annotator_group",
        "annotation_version",
        "quality_flag",
    }
    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise WorkbenchError(E_INPUT, "sample must be an object", {"index": index})
        missing = sorted(required - set(record))
        if missing:
            raise WorkbenchError(
                E_INPUT,
                "sample is missing required fields",
                {"index": index, "missing": missing},
            )
        pair_id = record["pair_id"]
        if not isinstance(pair_id, str) or not pair_id:
            raise WorkbenchError(E_INPUT, "pair_id must be a non-empty string", {"index": index})
        if pair_id in seen:
            raise WorkbenchError(E_INPUT, "duplicate pair_id", {"pair_id": pair_id})
        seen.add(pair_id)
        for field in ("prompt", "chosen", "rejected", "annotator_group", "quality_flag"):
            if not isinstance(record[field], str):
                raise WorkbenchError(
                    E_INPUT,
                    f"{field} must be a string",
                    {"pair_id": pair_id, "field": field},
                )
        annotation_version = record["annotation_version"]
        if isinstance(annotation_version, bool) or not isinstance(annotation_version, (int, str)):
            raise WorkbenchError(
                E_INPUT,
                "annotation_version must be an integer or string",
                {"pair_id": pair_id},
            )

    records.sort(key=lambda item: item["pair_id"])
    return records, hash_value(records)


def read_config(path: Path | str) -> tuple[dict[str, Any], str]:
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkbenchError(E_INPUT, "cannot read configuration", {"path": str(source), "error": str(exc)})
    config = validate_config(raw)
    return config, hash_value(config)


def mutate_samples(
    samples: list[dict[str, Any]],
    *,
    pair_id: str | None = None,
    annotator_group: str | None = None,
    chosen: str | None = None,
    rejected: str | None = None,
    quality_flag: str | None = None,
    chosen_suffix: str | None = None,
    rejected_suffix: str | None = None,
    bump_version: bool = True,
) -> list[dict[str, Any]]:
    if pair_id is None and annotator_group is None:
        raise WorkbenchError(E_INPUT, "mutate requires pair_id or annotator_group")
    result = copy.deepcopy(samples)
    matched = 0
    for record in result:
        if pair_id is not None and record["pair_id"] != pair_id:
            continue
        if annotator_group is not None and record["annotator_group"] != annotator_group:
            continue
        matched += 1
        if chosen is not None:
            record["chosen"] = chosen
        if rejected is not None:
            record["rejected"] = rejected
        if quality_flag is not None:
            record["quality_flag"] = quality_flag
        if chosen_suffix is not None:
            record["chosen"] = record["chosen"] + chosen_suffix
        if rejected_suffix is not None:
            record["rejected"] = record["rejected"] + rejected_suffix
        if bump_version and isinstance(record["annotation_version"], int):
            record["annotation_version"] += 1
    if matched == 0:
        raise WorkbenchError(
            E_INPUT,
            "mutation matched no samples",
            {"pair_id": pair_id, "annotator_group": annotator_group},
        )
    result.sort(key=lambda item: item["pair_id"])
    return result
