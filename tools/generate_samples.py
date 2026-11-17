#!/usr/bin/env python3
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path


SEED = 20261117
ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "sample_data"
FIXTURES = SAMPLES / "fixtures"

VOCAB = [
    "amber",
    "anchor",
    "basket",
    "beacon",
    "bridge",
    "canvas",
    "cedar",
    "cipher",
    "clover",
    "copper",
    "crystal",
    "delta",
    "ember",
    "falcon",
    "field",
    "forest",
    "garden",
    "harbor",
    "hollow",
    "island",
    "juniper",
    "kernel",
    "lantern",
    "lemon",
    "maple",
    "meadow",
    "meteor",
    "nectar",
    "ocean",
    "orbit",
    "pebble",
    "quartz",
    "river",
    "sparrow",
    "summit",
    "tundra",
    "velvet",
    "willow",
    "winter",
    "zephyr",
]


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def words(rng: random.Random, count: int) -> list[str]:
    return [rng.choice(VOCAB) for _ in range(count)]


def sentence(rng: random.Random, count: int, uppercase: bool = False) -> str:
    selected = words(rng, count)
    if uppercase and selected:
        selected[0] = selected[0].upper()
    return " ".join(selected) + "."


def make_pair(
    rng: random.Random,
    pair_id: str,
    annotator: str,
    timestamp: datetime,
) -> dict[str, object]:
    prompt_words = words(rng, rng.randint(7, 13))
    shared = words(rng, rng.randint(8, 14))
    chosen_extra = words(rng, rng.randint(3, 8))
    rejected_extra = words(rng, rng.randint(0, 5))
    return {
        "pair_id": pair_id,
        "prompt": " ".join(prompt_words) + "?",
        "chosen": " ".join(shared + chosen_extra) + ".",
        "rejected": " ".join(shared + rejected_extra) + ".",
        "annotator_id": annotator,
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
    }


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(canonical(record) + "\n" for record in records),
        encoding="utf-8",
    )


def main() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    start = datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc)
    pairs = [
        make_pair(
            rng,
            f"p{index:03d}",
            f"a{((index - 1) % 5) + 1:02d}",
            start + timedelta(hours=index, minutes=index * 3),
        )
        for index in range(1, 13)
    ]
    base_lines: list[str] = [canonical(pair) for pair in pairs]
    base_lines.append(
        canonical(
            {
                "pair_id": "p-no-timestamp",
                "prompt": "missing timestamp sample",
                "chosen": "amber beacon",
                "rejected": "cedar delta",
                "annotator_id": "a01",
            }
        )
    )
    base_lines.append(
        canonical(
            {
                "pair_id": "p-empty",
                "prompt": "empty text sample",
                "chosen": "   ",
                "rejected": "cedar delta",
                "annotator_id": "a02",
                "timestamp": "2026-01-06T00:00:00Z",
            }
        )
    )
    duplicate = dict(pairs[9])
    duplicate["chosen"] = duplicate["chosen"] + " duplicate record variant"
    base_lines.append(canonical(duplicate))
    (SAMPLES / "preferences.jsonl").write_text(
        "\n".join(base_lines) + "\n", encoding="utf-8"
    )

    config = {
        "format_version": 1,
        "subword_rule": "word_v1",
        "subword_pattern": "[a-z0-9]+",
        "normalize_text": True,
        "normalization_version": "nfkc_casefold_whitespace_v1",
        "feature_dimensions": 24,
        "hash_buckets": 24,
        "hash_function": "blake2b_64_v1",
        "projection": "bucket_mod_dimensions_v1",
        "weights": [
            0.31,
            -0.22,
            0.17,
            0.12,
            -0.09,
            0.08,
            0.25,
            -0.14,
            0.06,
            0.19,
            -0.11,
            0.04,
            0.27,
            -0.18,
            0.09,
            0.03,
            0.15,
            -0.07,
            0.21,
            -0.13,
            0.05,
            0.16,
            -0.20,
            0.11,
        ],
        "score_precision": 6,
        "reward_delta_threshold": 0.15,
        "concurrency": 4,
        "snapshot_commit_interval": 1,
    }
    write_json(SAMPLES / "feature_config.json", config)

    replace_p003 = make_pair(
        rng, "p003", "a03", start + timedelta(days=1, hours=2)
    )
    add_p013 = make_pair(
        rng, "p013", "a01", start + timedelta(days=1, hours=5)
    )
    replace_p007 = make_pair(
        rng, "p007", "a02", start + timedelta(days=2, hours=1)
    )
    add_p014 = make_pair(
        rng, "p014", "a05", start + timedelta(days=2, hours=4)
    )
    plan = {
        "format_version": 1,
        "batches": [
            {
                "batch_id": "B001",
                "replace": [replace_p003],
                "delete": ["p005"],
                "add": [add_p013],
            },
            {
                "batch_id": "B002",
                "weight_updates": {"0": 0.43, "5": -0.16, "12": 0.34},
            },
            {
                "batch_id": "B003",
                "reward_delta_threshold": 0.22,
            },
            {
                "batch_id": "B004",
                "replace": [replace_p007],
                "delete": ["p009"],
                "add": [add_p014],
                "weight_updates": {"2": 0.09, "16": -0.12},
                "reward_delta_threshold": 0.18,
            },
        ],
    }
    write_json(SAMPLES / "batch_plan.json", plan)

    dependencies = {
        "format_version": 1,
        "stage_order": [
            "load",
            "features",
            "scoring",
            "discrimination",
            "summary",
        ],
        "stage_dependencies": {
            "load": [],
            "features": ["load"],
            "scoring": ["features", "weights", "precision"],
            "discrimination": ["scoring", "threshold"],
            "summary": ["load", "features", "scoring", "discrimination"],
        },
        "summary_dependencies": {
            "boundary_samples": {
                "batches": "*",
                "buckets": "*",
                "stages": ["discrimination", "threshold"],
            },
            "clear_pairs": {
                "batches": "*",
                "buckets": "*",
                "stages": ["discrimination", "threshold"],
            },
            "reward_delta_distribution": {
                "batches": "*",
                "buckets": [],
                "stages": ["scoring", "weights", "precision"],
            },
            "bucket_frequency": {
                "batches": "*",
                "buckets": "*",
                "stages": ["features"],
            },
            "annotator_groups": {
                "batches": "*",
                "buckets": "*",
                "stages": ["features"],
            },
            "failure_list": {
                "batches": "*",
                "buckets": [],
                "stages": ["load", "features", "scoring", "discrimination"],
            },
        },
        "edges": [
            {"from": "load", "to": "features"},
            {"from": "features", "to": "scoring"},
            {"from": "weights", "to": "scoring"},
            {"from": "scoring", "to": "discrimination"},
            {"from": "threshold", "to": "discrimination"},
            {"from": "features", "to": "summary:bucket_frequency"},
            {"from": "scoring", "to": "summary:reward_delta_distribution"},
            {"from": "discrimination", "to": "summary:boundary_samples"},
            {"from": "load", "to": "summary:failure_list"},
        ],
    }
    write_json(SAMPLES / "dependencies.json", dependencies)

    missing_fixture = {
        "pair_id": "INJ-MISSING-001",
        "prompt": "missing timestamp injection",
        "chosen": "amber beacon",
        "rejected": "cedar delta",
        "annotator_id": "inj-a01",
    }
    write_jsonl(FIXTURES / "missing_field.jsonl", [missing_fixture])
    write_jsonl(
        FIXTURES / "empty_text.jsonl",
        [
            {
                "pair_id": "INJ-EMPTY-001",
                "prompt": "empty chosen injection",
                "chosen": "",
                "rejected": "cedar delta",
                "annotator_id": "inj-a02",
                "timestamp": "2026-02-01T00:00:00Z",
            }
        ],
    )
    threshold_spec = {
        "pair_id": "INJ-THRESHOLD-EQ-001",
        "feature_dimensions": 8,
        "hash_buckets": 8,
        "weights": [0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25],
        "score_precision": 6,
        "reward_delta_threshold": 0.5,
        "chosen_vector": [2, 0, 0, 0, 0, 0, 0, 0],
        "rejected_vector": [0, 0, 0, 0, 0, 0, 0, 0],
    }
    write_json(FIXTURES / "threshold_equal.json", threshold_spec)
    similarity_records = [
        {
            "pair_id": "INJ-SIM-MAX-1",
            "prompt": "similarity max one",
            "chosen": "amber beacon cedar",
            "rejected": "amber beacon cedar",
            "annotator_id": "INJ-MAX",
            "timestamp": "2026-02-01T00:01:00Z",
        },
        {
            "pair_id": "INJ-SIM-MAX-2",
            "prompt": "similarity max two",
            "chosen": "amber beacon cedar",
            "rejected": "amber beacon cedar",
            "annotator_id": "INJ-MAX",
            "timestamp": "2026-02-01T00:02:00Z",
        },
        {
            "pair_id": "INJ-SIM-MIN-1",
            "prompt": "similarity min one",
            "chosen": "amber beacon",
            "rejected": "cedar delta",
            "annotator_id": "INJ-MIN",
            "timestamp": "2026-02-01T00:03:00Z",
        },
        {
            "pair_id": "INJ-SIM-MIN-2",
            "prompt": "similarity min two",
            "chosen": "forest garden",
            "rejected": "harbor island",
            "annotator_id": "INJ-MIN",
            "timestamp": "2026-02-01T00:04:00Z",
        },
    ]
    write_jsonl(FIXTURES / "similarity_extrema.jsonl", similarity_records)
    empty_subword = {
        "pair_id": "INJ-EMPTY-SUBWORD-001",
        "prompt": "!!!",
        "chosen": "???",
        "rejected": "...",
        "annotator_id": "INJ-SUBWORD",
        "timestamp": "2026-02-02T00:00:00Z",
    }
    write_jsonl(FIXTURES / "empty_subword.jsonl", [empty_subword])
    illegal_weight = {
        "subword_rule": "word_v1",
        "subword_pattern": "[a-z0-9]+",
        "normalize_text": True,
        "feature_dimensions": 4,
        "hash_buckets": 4,
        "weights": ["not-a-number", 0.0, 0.0, 0.0],
        "score_precision": 6,
        "reward_delta_threshold": 0.1,
        "concurrency": 2,
        "snapshot_commit_interval": 1,
    }
    write_json(FIXTURES / "illegal_weight.json", illegal_weight)
    stage_exception = {
        "target_pair_id": "INJ-EXCEPTION-001",
        "fault_map": {"INJ-EXCEPTION-001": "scoring"},
        "config": {
            "subword_rule": "word_v1",
            "subword_pattern": "[a-z0-9]+",
            "normalize_text": True,
            "feature_dimensions": 8,
            "hash_buckets": 8,
            "weights": [0.2, -0.1, 0.3, 0.05, -0.2, 0.1, 0.15, -0.05],
            "score_precision": 6,
            "reward_delta_threshold": 0.1,
            "concurrency": 2,
            "snapshot_commit_interval": 1,
        },
        "pairs": [
            {
                "pair_id": "INJ-EXCEPTION-001",
                "prompt": "exception isolation target",
                "chosen": "amber beacon",
                "rejected": "cedar delta",
                "annotator_id": "INJ-EXCEPTION",
                "timestamp": "2026-02-03T00:00:00Z",
            },
            {
                "pair_id": "INJ-EXCEPTION-002",
                "prompt": "exception isolation healthy",
                "chosen": "forest garden",
                "rejected": "harbor island",
                "annotator_id": "INJ-EXCEPTION",
                "timestamp": "2026-02-03T00:01:00Z",
            },
        ],
    }
    write_json(FIXTURES / "stage_exception.json", stage_exception)
    injection_config = {
        "format_version": 1,
        "items": [
            {
                "injection_id": "INJ-001",
                "kind": "missing_field",
                "fixture": "fixtures/missing_field.jsonl",
            },
            {
                "injection_id": "INJ-002",
                "kind": "empty_text",
                "fixture": "fixtures/empty_text.jsonl",
            },
            {
                "injection_id": "INJ-003",
                "kind": "threshold_equal",
                "fixture": "fixtures/threshold_equal.json",
            },
            {
                "injection_id": "INJ-004",
                "kind": "similarity_extrema",
                "fixture": "fixtures/similarity_extrema.jsonl",
            },
            {
                "injection_id": "INJ-005",
                "kind": "empty_subword",
                "fixture": "fixtures/empty_subword.jsonl",
            },
            {
                "injection_id": "INJ-006",
                "kind": "illegal_weight",
                "fixture": "fixtures/illegal_weight.json",
            },
            {
                "injection_id": "INJ-007",
                "kind": "pair_stage_exception",
                "fixture": "fixtures/stage_exception.json",
            },
        ],
    }
    write_json(SAMPLES / "injection_config.json", injection_config)


if __name__ == "__main__":
    main()
