"""Deterministic local sample generation.

The sample deliberately contains:

* repeated events across batches,
* a bridge event with evidence toward two existing chains,
* a same-role argument conflict with different source trust,
* unrelated chains that must be reused untouched,
* time aliases, trigger variants, and Unicode text.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

from .canonical import atomic_write_text, canonical_json, pretty_json, sha256_bytes


SEED = 20261124

ENTITY_DICTIONARY = {
    "schema_version": "entity-dictionary/v1",
    "roles": {
        "ORG": "组织",
        "PRODUCT": "产品",
        "PERSON": "人物",
        "LOCATION": "地点",
        "TOPIC": "主题",
    },
    "entities": [
        {"name": "Nova Robotics", "role": "ORG"},
        {"name": "Aurora X1", "role": "PRODUCT"},
        {"name": "星云一号", "role": "PRODUCT"},
        {"name": "Orion Cloud", "role": "PRODUCT"},
        {"name": "Alice Chen", "role": "PERSON"},
        {"name": "Alicia Chen", "role": "PERSON"},
        {"name": "Shanghai", "role": "LOCATION"},
        {"name": "Beijing", "role": "LOCATION"},
        {"name": "semiconductor supply", "role": "TOPIC"},
    ],
}

EVENT_TYPE_TABLE = {
    "schema_version": "event-type-table/v1",
    "event_types": [
        "PRODUCT_LAUNCH",
        "SERVICE_OUTAGE",
        "LEADERSHIP_CHANGE",
        "POLICY_ANNOUNCEMENT",
        "MARKET_MOVE",
    ],
}

RULES_PAYLOAD: dict[str, Any] = {
    "schema_version": "coref-rules/v1",
    "weights": {
        "version": 1,
        "entity": 0.45,
        "time": 0.2,
        "trigger": 0.2,
        "source_trust": 0.15,
    },
    "entity_role_weights": {
        "ORG": 1.0,
        "PRODUCT": 1.0,
        "PERSON": 0.95,
        "LOCATION": 0.7,
        "TOPIC": 0.6,
    },
    "time_equivalence": {
        "version": 1,
        "groups": [
            ["2026-11-20", "11月20日", "Nov 20, 2026"],
            ["2026-11-21", "11月21日", "Nov 21, 2026"],
            ["2026-11-22", "11月22日", "Nov 22, 2026"],
            ["2026-12-01", "12月1日", "Dec 1, 2026"],
        ],
    },
    "trigger_similarity": {
        "version": 1,
        "method": "char_bigram_jaccard",
        "casefold": True,
    },
    "candidate_floor": 0.55,
    "degraded_candidate_floor": 0.78,
    "chain_priority": ["evidence_total", "chain_size", "first_seen"],
    "resource_limits": {
        "max_batch_events": 100,
        "max_pair_comparisons": 50000,
        "max_resident_chain_mb": 128,
    },
    "resource_limit_mode": "error",
}

TIME_ALIASES = {
    "2026-11-20": ["2026-11-20", "11月20日", "Nov 20, 2026"],
    "2026-11-21": ["2026-11-21", "11月21日", "Nov 21, 2026"],
    "2026-11-22": ["2026-11-22", "11月22日", "Nov 22, 2026"],
    "2026-12-01": ["2026-12-01", "12月1日", "Dec 1, 2026"],
}

TRIGGER_ALIASES = {
    "PRODUCT_LAUNCH": ["发布", "推出", "launch"],
    "SERVICE_OUTAGE": ["故障", "宕机", "outage"],
    "LEADERSHIP_CHANGE": ["任命", "宣布", "appoint"],
    "POLICY_ANNOUNCEMENT": ["发布", "宣布", "announce"],
    "MARKET_MOVE": ["上涨", "波动", "surge"],
}


def _event(
    event_id: str,
    batch_no: int,
    event_type: str,
    entities: list[tuple[str, str]],
    time_expr: str,
    triggers: list[str],
    source_trust: float,
) -> dict[str, Any]:
    mentions = []
    for index, trigger in enumerate(triggers):
        start = 0 if index == 0 else 1
        mentions.append(
            {"trigger": trigger, "start": start, "end": start + len(trigger)}
        )
    return {
        "event_id": event_id,
        "doc_id": f"doc-{batch_no:02d}-{event_id[-3:]}",
        "batch_no": batch_no,
        "event_type": event_type,
        "entities": [{"name": name, "role": role} for name, role in entities],
        "time_expr": time_expr,
        "mentions": mentions,
        "source_trust": source_trust,
    }


def _base_events() -> list[dict[str, Any]]:
    return [
        _event(
            "ev-0001",
            1,
            "PRODUCT_LAUNCH",
            [("Nova Robotics", "ORG"), ("Aurora X1", "PRODUCT")],
            "2026-11-20",
            ["发布"],
            0.95,
        ),
        _event(
            "ev-0002",
            1,
            "PRODUCT_LAUNCH",
            [("Nova Robotics", "ORG"), ("Aurora X1", "PRODUCT")],
            "11月20日",
            ["推出"],
            0.84,
        ),
        _event(
            "ev-0003",
            1,
            "SERVICE_OUTAGE",
            [("Orion Cloud", "PRODUCT"), ("Shanghai", "LOCATION")],
            "2026-11-21",
            ["故障"],
            0.9,
        ),
        _event(
            "ev-0004",
            1,
            "POLICY_ANNOUNCEMENT",
            [("Beijing", "LOCATION"), ("semiconductor supply", "TOPIC")],
            "2026-11-22",
            ["宣布"],
            0.72,
        ),
        _event(
            "ev-0005",
            2,
            "PRODUCT_LAUNCH",
            [("Nova Robotics", "ORG"), ("Aurora X1", "PRODUCT")],
            "Nov 20, 2026",
            ["launch"],
            0.78,
        ),
        _event(
            "ev-0006",
            2,
            "SERVICE_OUTAGE",
            [("Orion Cloud", "PRODUCT"), ("Shanghai", "LOCATION")],
            "11月21日",
            ["宕机"],
            0.88,
        ),
        _event(
            "ev-0007",
            2,
            "LEADERSHIP_CHANGE",
            [("Nova Robotics", "ORG"), ("Alice Chen", "PERSON")],
            "11月22日",
            ["宣布"],
            0.82,
        ),
        _event(
            "ev-0008",
            2,
            "MARKET_MOVE",
            [("semiconductor supply", "TOPIC")],
            "2026-12-01",
            ["波动"],
            0.65,
        ),
        _event(
            "ev-0009",
            3,
            "LEADERSHIP_CHANGE",
            [("Nova Robotics", "ORG"), ("Alicia Chen", "PERSON")],
            "11月20日",
            ["宣布"],
            0.99,
        ),
        _event(
            "ev-0010",
            3,
            "PRODUCT_LAUNCH",
            [("Nova Robotics", "ORG"), ("星云一号", "PRODUCT")],
            "2026-11-20",
            ["发布"],
            0.99,
        ),
        _event(
            "ev-0011",
            4,
            "SERVICE_OUTAGE",
            [("Orion Cloud", "PRODUCT"), ("Shanghai", "LOCATION")],
            "2026-11-21",
            ["outage"],
            0.86,
        ),
        _event(
            "ev-0012",
            4,
            "POLICY_ANNOUNCEMENT",
            [("Beijing", "LOCATION"), ("semiconductor supply", "TOPIC")],
            "2026-11-22",
            ["发布"],
            0.76,
        ),
        _event(
            "ev-0013",
            4,
            "MARKET_MOVE",
            [("semiconductor supply", "TOPIC")],
            "12月1日",
            ["上涨"],
            0.7,
        ),
        _event(
            "ev-0014",
            4,
            "LEADERSHIP_CHANGE",
            [("Nova Robotics", "ORG"), ("Alice Chen", "PERSON")],
            "11月22日",
            ["宣布"],
            0.81,
        ),
    ]


def _noise_event(
    event_id: str, batch_no: int, rng: random.Random, ordinal: int
) -> dict[str, Any]:
    event_types = [
        "PRODUCT_LAUNCH",
        "SERVICE_OUTAGE",
        "LEADERSHIP_CHANGE",
        "POLICY_ANNOUNCEMENT",
        "MARKET_MOVE",
    ]
    event_type = event_types[ordinal % len(event_types)]
    entity_pool = ENTITY_DICTIONARY["entities"]
    picked = [entity_pool[(ordinal * 2) % len(entity_pool)]]
    if ordinal % 3 == 0:
        picked.append(entity_pool[(ordinal * 2 + 3) % len(entity_pool)])
    entities = [(item["name"], item["role"]) for item in picked]
    times = list(TIME_ALIASES)
    time_expr = rng.choice(TIME_ALIASES[times[ordinal % len(times)]])
    triggers = TRIGGER_ALIASES[event_type]
    trust = round(rng.uniform(0.45, 0.74), 2)
    return _event(
        event_id,
        batch_no,
        event_type,
        entities,
        time_expr,
        [rng.choice(triggers)],
        trust,
    )


def build_events(
    seed: int = SEED, event_count: int = 14, batch_count: int = 4
) -> list[dict[str, Any]]:
    if event_count < 1:
        raise ValueError("event_count must be positive")
    if batch_count < 1:
        raise ValueError("batch_count must be positive")
    rng = random.Random(seed)
    events = _base_events()
    while len(events) < event_count:
        ordinal = len(events) + 1
        events.append(
            _noise_event(
                f"ev-{ordinal:04d}",
                ((ordinal - 1) % batch_count) + 1,
                rng,
                ordinal,
            )
        )
    events = events[:event_count]
    if batch_count < 4:
        for event in events:
            event["batch_no"] = ((event["batch_no"] - 1) % batch_count) + 1
    document_offset = rng.randrange(1, 10000)
    for index, event in enumerate(events):
        event["doc_id"] = (
            f"doc-{event['batch_no']:02d}-{(document_offset + index) % 10000:04d}"
        )
    events.sort(key=lambda item: (item["batch_no"], item["event_id"]))
    return events


def write_events_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    lines = [canonical_json(event) for event in events]
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def generate_sample(
    output_dir: str | Path,
    *,
    seed: int = SEED,
    event_count: int = 14,
    batch_count: int = 4,
    force: bool = False,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()) and not force:
        raise FileExistsError(
            f"{output_dir} is not empty; pass force=True to replace sample files"
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    events = build_events(seed=seed, event_count=event_count, batch_count=batch_count)
    events_path = output_dir / "events.jsonl"
    write_events_jsonl(events_path, events)
    atomic_write_text(
        output_dir / "entity_dictionary.json",
        pretty_json(ENTITY_DICTIONARY),
    )
    atomic_write_text(
        output_dir / "event_types.json",
        pretty_json(EVENT_TYPE_TABLE),
    )
    atomic_write_text(
        output_dir / "coref_rules.json",
        pretty_json(RULES_PAYLOAD),
    )
    atomic_write_text(output_dir / "seed.txt", f"{seed}\n")

    files = [
        "events.jsonl",
        "entity_dictionary.json",
        "event_types.json",
        "coref_rules.json",
        "seed.txt",
    ]
    manifest = {
        "schema_version": "sample-manifest/v1",
        "seed": seed,
        "event_count": len(events),
        "batch_count": batch_count,
        "files": {
            name: sha256_bytes((output_dir / name).read_bytes()) for name in files
        },
    }
    atomic_write_text(output_dir / "manifest.json", pretty_json(manifest))
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate deterministic coreference samples")
    parser.add_argument("--out", required=True, help="output sample directory")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--events", type=int, default=14)
    parser.add_argument("--batches", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = generate_sample(
        args.out,
        seed=args.seed,
        event_count=args.events,
        batch_count=args.batches,
        force=args.force,
    )
    print(pretty_json(manifest), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

