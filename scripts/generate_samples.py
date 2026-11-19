#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from qrw.canonical import checksum_tag, write_json, write_text  # noqa: E402
from qrw.readers import load_slot_types  # noqa: E402
from qrw.snapshot import snapshot_payload  # noqa: E402


DEFAULT_SEED = 20261120


SLOT_TYPES: dict[str, Any] = {
    "version": "1.0",
    "types": {
        "GEO": {"values": ["上海", "巴黎", "杭州", "北京"]},
        "PROPERTY": {"values": ["酒店", "民宿"]},
        "INTENT": {"values": ["推荐", "预订"]},
        "AREA": {"values": ["外滩", "西湖", "市中心"]},
    },
}


RULES: list[dict[str, Any]] = [
    {
        "id": "R_rank",
        "pattern": "{city} {property} 推荐",
        "replacement": "{city} {property} 榜单",
        "slots": [("city", "GEO"), ("property", "PROPERTY")],
        "weight": 1.0,
        "enabled": True,
        "max_depth": 1,
        "order": 10,
    },
    {
        "id": "R_book",
        "pattern": "{city} {property} 预订",
        "replacement": "{city} {property} 立即预订",
        "slots": [("city", "GEO"), ("property", "PROPERTY")],
        "weight": 1.0,
        "enabled": True,
        "max_depth": 1,
        "order": 20,
    },
    {
        "id": "R_intent",
        "pattern": "{city} {property} {intent}",
        "replacement": "{city} 热门 {property} {intent}",
        "slots": [("city", "GEO"), ("property", "PROPERTY"), ("intent", "INTENT")],
        "weight": 1.0,
        "enabled": True,
        "max_depth": 1,
        "order": 30,
    },
    {
        "id": "R_area",
        "pattern": "{city} {area} {property} 推荐",
        "replacement": "{city} {area} 附近 {property} 推荐",
        "slots": [("city", "GEO"), ("area", "AREA"), ("property", "PROPERTY")],
        "weight": 1.0,
        "enabled": True,
        "max_depth": 1,
        "order": 40,
    },
]


ADVANCED_RULES_V3: dict[str, Any] = {
    "version": "V3",
    "template_set_id": "advanced_conditional_reference_demo",
    "rules": [
        {
            "id": "hotel_recommend_conditional",
            "kind": "rule",
            "priority": 10,
            "weight": 1.0,
            "enabled": True,
            "max_depth": 2,
            "pattern": "{city} {property} 推荐",
            "slots": [
                {"name": "city", "type": "GEO", "source": "query"},
                {"name": "property", "type": "PROPERTY", "source": "query"},
            ],
            "replacement": [
                {"text": "{city} {property} "},
                {
                    "if": {"slot": "property", "operator": "equals", "value": "酒店"},
                    "then": [{"text": "榜单"}],
                    "else": [{"text": "精选"}],
                },
            ],
        },
        {
            "id": "brand_suffix",
            "kind": "fragment",
            "priority": 0,
            "weight": 1.0,
            "enabled": True,
            "max_depth": 2,
            "slots": [],
            "replacement": [{"text": "｜本地优选"}],
        },
        {
            "id": "booking_with_ref",
            "kind": "rule",
            "priority": 20,
            "weight": 1.0,
            "enabled": True,
            "max_depth": 2,
            "pattern": "{city} {property} 预订",
            "slots": [
                {"name": "city", "type": "GEO", "source": "query"},
                {"name": "property", "type": "PROPERTY", "source": "query"},
            ],
            "replacement": [
                {"text": "{city} {property} "},
                {"ref": "brand_suffix"},
            ],
        },
    ],
}


ADVANCED_FALLBACK_RULES: list[dict[str, Any]] = [
    {
        "id": "hotel_recommend_conditional",
        "pattern": "{city} {property} 推荐",
        "replacement": "{city} {property} 榜单",
        "slots": [("city", "GEO"), ("property", "PROPERTY")],
        "weight": 1.0,
        "enabled": True,
        "max_depth": 2,
        "order": 10,
    },
    {
        "id": "booking_with_ref",
        "pattern": "{city} {property} 预订",
        "replacement": "{city} {property} 立即预订",
        "slots": [("city", "GEO"), ("property", "PROPERTY")],
        "weight": 1.0,
        "enabled": True,
        "max_depth": 2,
        "order": 20,
    },
]


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _jsonl_text(items: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in items
    )


def _rules_v1(rules: list[dict[str, Any]]) -> str:
    lines = ["#V1"]
    for rule in rules:
        lines.append(f"{rule['pattern']}|{rule['replacement']}|{rule['order']}")
    return "\n".join(lines) + "\n"


def _rules_v2(rules: list[dict[str, Any]]) -> str:
    lines = ["#V2"]
    for rule in rules:
        slots = ",".join(f"{name}:{slot_type}" for name, slot_type in rule["slots"]) or "-"
        values = (
            ("id", json.dumps(rule["id"], ensure_ascii=False)),
            ("pattern", json.dumps(rule["pattern"], ensure_ascii=False)),
            ("replacement", json.dumps(rule["replacement"], ensure_ascii=False)),
            ("slots", json.dumps(slots, ensure_ascii=False)),
            ("weight", str(rule["weight"])),
            ("enabled", "true" if rule["enabled"] else "false"),
            ("max_depth", str(rule["max_depth"])),
            ("order", str(rule["order"])),
        )
        lines.append("; ".join(f"{key}={value}" for key, value in values))
    return "\n".join(lines) + "\n"


def _rules_v3(rules: list[dict[str, Any]], template_set_id: str) -> dict[str, Any]:
    return {
        "version": "V3",
        "template_set_id": template_set_id,
        "rules": [
            {
                "id": rule["id"],
                "kind": "rule",
                "priority": rule["order"],
                "weight": rule["weight"],
                "enabled": rule["enabled"],
                "max_depth": rule["max_depth"],
                "pattern": rule["pattern"],
                "slots": [
                    {"name": name, "type": slot_type, "source": "query"}
                    for name, slot_type in rule["slots"]
                ],
                "replacement": [{"text": rule["replacement"]}],
            }
            for rule in rules
        ],
    }


def _write_bundle(
    directory: Path,
    rules: list[dict[str, Any]],
    slot_types: dict[str, Any],
    template_set_id: str,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    write_text(directory / "v1.txt", _rules_v1(rules))
    write_text(directory / "v2.txt", _rules_v2(rules))
    write_json(directory / "v3.json", _rules_v3(rules, template_set_id))
    write_json(directory / "slot_types.json", slot_types)


def _snapshot_manifest(
    *,
    version: int,
    set_id: str,
    source_dir: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    sources = {
        dialect: (source_dir / f"{dialect}.txt").read_text(encoding="utf-8")
        if dialect in ("v1", "v2")
        else (source_dir / f"{dialect}.json").read_text(encoding="utf-8")
        for dialect in ("v1", "v2", "v3")
    }
    slot_types = load_slot_types(source_dir / "slot_types.json")
    checksum = checksum_tag(
        snapshot_payload(
            version=version,
            set_id=set_id,
            slot_types=slot_types,
            sources=sources,
        )
    )
    relative_dir = Path("..") / source_dir.relative_to(source_dir.parents[1])
    return {
        "format": "qrw-snapshot/1",
        "version": version,
        "set_id": set_id,
        "checksum": checksum,
        "dialect_files": {
            "v1": str(relative_dir / "v1.txt").replace("\\", "/"),
            "v2": str(relative_dir / "v2.txt").replace("\\", "/"),
            "v3": str(relative_dir / "v3.json").replace("\\", "/"),
        },
        "slot_types_file": str(relative_dir / "slot_types.json").replace("\\", "/"),
        "created_from": "scripts/generate_samples.py",
    }


def _write_anomalies(samples: Path, slot_types: dict[str, Any]) -> None:
    anomaly_dir = samples / "anomalies"
    anomaly_dir.mkdir(parents=True, exist_ok=True)
    write_text(anomaly_dir / "invalid_version.txt", "#V9\n{city}|{city} 推荐|10\n")
    write_text(anomaly_dir / "bad_pipes.txt", "#V1\n{city} {property} 推荐|{city} 榜单\n")
    write_text(anomaly_dir / "empty_pattern.txt", "#V1\n|{city} 榜单|10\n")
    write_text(
        anomaly_dir / "duplicate_queries.jsonl",
        _jsonl_text(
            [
            {
                "query_id": "duplicate",
                "query_text": "上海 酒店 推荐",
                "locale": "zh_CN",
                "template_set_version": "2",
            },
            {
                "query_id": "duplicate",
                "query_text": "北京 民宿 推荐",
                "locale": "zh_CN",
                "template_set_version": "2",
            },
            ]
        ),
    )
    write_json(
        anomaly_dir / "bad_slot_type.json",
        {
            "version": "V3",
            "template_set_id": "bad_slot_type",
            "rules": [
                {
                    "id": "bad_geo",
                    "priority": 10,
                    "weight": 1.0,
                    "enabled": True,
                    "max_depth": 1,
                    "pattern": "{city} 酒店 推荐",
                    "slots": [{"name": "city", "type": "COUNTRY"}],
                    "replacement": [{"text": "{city} 国际酒店榜单"}],
                }
            ],
        },
    )
    write_json(
        anomaly_dir / "over_depth.json",
        {
            "version": "V3",
            "template_set_id": "over_depth",
            "rules": [
                {
                    "id": "deep_root",
                    "priority": 10,
                    "weight": 1.0,
                    "enabled": True,
                    "max_depth": 1,
                    "pattern": "{city}",
                    "slots": [{"name": "city", "type": "GEO"}],
                    "replacement": [{"text": "{city} "}, {"ref": "deep_l1"}],
                },
                {
                    "id": "deep_l1",
                    "kind": "fragment",
                    "priority": 0,
                    "weight": 1.0,
                    "enabled": True,
                    "max_depth": 1,
                    "slots": [],
                    "replacement": [{"ref": "deep_l2"}],
                },
                {
                    "id": "deep_l2",
                    "kind": "fragment",
                    "priority": 0,
                    "weight": 1.0,
                    "enabled": True,
                    "max_depth": 1,
                    "slots": [],
                    "replacement": [{"text": "complete"}],
                },
            ],
        },
    )
    write_json(anomaly_dir / "slot_types.json", slot_types)


def generate_samples(root: Path, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    samples = root / "samples"
    samples.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    query_items = [
        {
            "query_id": "q001",
            "query_text": "上海 酒店 推荐",
            "locale": "zh_CN",
            "template_set_version": "2",
        },
        {
            "query_id": "q002",
            "query_text": "巴黎 酒店 预订",
            "locale": "fr_FR",
            "template_set_version": "2",
        },
        {
            "query_id": "q003",
            "query_text": "杭州 民宿 推荐",
            "locale": "zh_CN",
            "template_set_version": "2",
        },
        {
            "query_id": "q004",
            "query_text": "上海 外滩 酒店 推荐",
            "locale": "zh_CN",
            "template_set_version": "2",
        },
        {
            "query_id": "q005",
            "query_text": "北京 民宿 预订",
            "locale": "zh_CN",
            "template_set_version": "2",
        },
    ]
    rng.shuffle(query_items)
    write_text(samples / "queries.jsonl", _jsonl_text(query_items))
    _write_bundle(samples, RULES, SLOT_TYPES, "core_equivalent_v2")

    snapshot_sources = samples / "snapshot_sources"
    _write_bundle(snapshot_sources / "v1", RULES[:3], SLOT_TYPES, "core_equivalent_v1")
    _write_bundle(snapshot_sources / "v2", RULES, SLOT_TYPES, "core_equivalent_v2")
    retired_types = json.loads(json.dumps(SLOT_TYPES, ensure_ascii=False))
    del retired_types["types"]["AREA"]
    _write_bundle(snapshot_sources / "retired", RULES, retired_types, "retired_area_type")

    snapshots = samples / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    snapshot_1 = _snapshot_manifest(
        version=1,
        set_id="core_equivalent",
        source_dir=snapshot_sources / "v1",
        manifest_path=snapshots / "snapshot_1.json",
    )
    snapshot_2 = _snapshot_manifest(
        version=2,
        set_id="core_equivalent",
        source_dir=snapshot_sources / "v2",
        manifest_path=snapshots / "snapshot_2.json",
    )
    snapshot_retired = _snapshot_manifest(
        version=3,
        set_id="core_equivalent",
        source_dir=snapshot_sources / "retired",
        manifest_path=snapshots / "snapshot_retired.json",
    )
    snapshot_retired["intent"] = "demonstrates E_SLOT_TYPE_RETIRED; not in the normal version index"
    write_json(snapshots / "snapshot_1.json", snapshot_1)
    write_json(snapshots / "snapshot_2.json", snapshot_2)
    write_json(snapshots / "snapshot_retired.json", snapshot_retired)
    write_json(
        samples / "versions.json",
        {
            "format": "qrw-version-index/1",
            "current_version": 2,
            "snapshots": [
                {"version": 1, "manifest": "snapshots/snapshot_1.json"},
                {"version": 2, "manifest": "snapshots/snapshot_2.json"},
            ],
        },
    )

    advanced = samples / "advanced"
    advanced.mkdir(parents=True, exist_ok=True)
    write_text(advanced / "v1.txt", _rules_v1(ADVANCED_FALLBACK_RULES))
    write_text(advanced / "v2.txt", _rules_v2(ADVANCED_FALLBACK_RULES))
    write_json(advanced / "v3.json", ADVANCED_RULES_V3)
    write_json(advanced / "slot_types.json", SLOT_TYPES)
    _write_anomalies(samples, SLOT_TYPES)
    write_json(
        samples / "generation_meta.json",
        {
            "generator": "scripts/generate_samples.py",
            "seed": seed,
            "deterministic": True,
            "query_count": len(query_items),
            "current_template_version": 2,
        },
    )
    return {
        "root": str(root),
        "seed": seed,
        "samples": str(samples),
        "queries": len(query_items),
        "current_version": 2,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic local query rewrite samples")
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="project root")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    result = generate_samples(Path(args.root).resolve(), args.seed)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

