"""Deterministic local multi-language sample generation."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from .bridge import serialize_bridge
from .models import (
    BridgeEntry,
    BridgeResource,
    Condition,
    LanguagePair,
    RewriteRule,
    RuleSet,
    Target,
)
from .rules import serialize_ruleset
from .util import atomic_write_bytes, sha256_bytes, stable_json_bytes

DEFAULT_SEED = 20261121


def generate_samples(
    root: str | Path,
    *,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    templates: dict[str, list[list[str]]] = {
        "zh": [
            ["汽车", "电动车", "电池", "车辆"],
            ["汽车", "发动机", "车辆"],
            ["自行车", "城市"],
            ["混合动力", "汽车", "电池"],
            ["智能", "汽车", "软件"],
            ["卡车", "柴油", "车辆"],
        ],
        "en": [
            ["car", "electric", "battery", "vehicle"],
            ["car", "engine", "vehicle"],
            ["bicycle", "city"],
            ["hybrid", "car", "battery"],
            ["smart", "car", "software"],
            ["truck", "diesel", "vehicle"],
        ],
        "fr": [
            ["voiture", "électrique", "batterie", "véhicule"],
            ["voiture", "moteur", "véhicule"],
            ["vélo", "ville"],
            ["hybride", "voiture", "batterie"],
            ["intelligent", "voiture", "logiciel"],
            ["camion", "diesel", "véhicule"],
        ],
        "es": [
            ["coche", "eléctrico", "batería", "vehículo"],
            ["coche", "motor", "ciudad"],
        ],
    }

    documents: list[dict[str, Any]] = []
    for lang, language_templates in templates.items():
        for index, terms in enumerate(language_templates, start=1):
            term_rows = [
                {"term": term, "freq": rng.randint(1, 4)}
                for term in sorted(terms)
            ]
            documents.append(
                {
                    "doc_id": f"doc-{lang}-{index:03d}",
                    "doc_lang": lang,
                    "terms": term_rows,
                    "doc_len": sum(row["freq"] for row in term_rows),
                }
            )
    documents.sort(key=lambda item: item["doc_id"])

    queries: list[dict[str, Any]] = [
        _query("q-zh-car", "zh", [("汽车", 1)], 5),
        _query("q-zh-car-electric", "zh", [("汽车", 1), ("电动车", 2)], 5),
        _query("q-zh-truck", "zh", [("卡车", 1)], 5),
        _query("q-en-car", "en", [("car", 1)], 5),
        _query("q-en-vehicle", "en", [("vehicle", 1)], 5),
        _query("q-fr-car", "fr", [("voiture", 1)], 5),
        _query("q-zh-bicycle", "zh", [("自行车", 1)], 5),
        _query("q-es-car", "es", [("coche", 1)], 5),
    ]
    query_pool = {
        "zh": ["汽车", "车辆", "电池", "智能", "城市", "卡车"],
        "en": ["car", "vehicle", "battery", "city", "truck"],
        "fr": ["voiture", "véhicule", "batterie", "ville", "camion"],
        "es": ["coche", "batería", "ciudad"],
    }
    for index in range(1, 5):
        lang = ("zh", "en", "fr", "es")[(index - 1) % 4]
        selected = rng.sample(query_pool[lang], k=min(2, len(query_pool[lang])))
        queries.append(
            _query(
                f"q-rand-{index:03d}",
                lang,
                [(term, rng.randint(1, 2)) for term in selected],
                rng.randint(3, 5),
            )
        )
    queries.sort(key=lambda item: item["query_id"])

    documents_path = root_path / "documents.jsonl"
    queries_path = root_path / "queries.jsonl"
    atomic_write_bytes(
        documents_path,
        b"".join(stable_json_bytes(document) for document in documents),
    )
    atomic_write_bytes(
        queries_path,
        b"".join(stable_json_bytes(query) for query in queries),
    )

    bridge_v1 = _bridge_v1()
    bridge_v2 = _bridge_v2()
    bridge_v3 = _bridge_v3()
    bridge_paths = {
        "bridge_v1": root_path / "bridge_v1.json",
        "bridge_v2": root_path / "bridge_v2.json",
        "bridge_v3": root_path / "bridge_v3.json",
    }
    for name, resource in (
        ("bridge_v1", bridge_v1),
        ("bridge_v2", bridge_v2),
        ("bridge_v3", bridge_v3),
    ):
        atomic_write_bytes(bridge_paths[name], serialize_bridge(resource))

    rules_v1 = _rules_v1()
    rules_v2 = _rules_v2()
    rules_v3 = _rules_v3()
    rule_paths = {
        "rules_v1": root_path / "rules_v1.rules",
        "rules_v2": root_path / "rules_v2.rules",
        "rules_v3": root_path / "rules_v3.json",
    }
    atomic_write_bytes(rule_paths["rules_v1"], serialize_ruleset(rules_v1))
    atomic_write_bytes(rule_paths["rules_v2"], serialize_ruleset(rules_v2))
    atomic_write_bytes(rule_paths["rules_v3"], serialize_ruleset(rules_v3))

    sample_files = {
        **{key: path for key, path in bridge_paths.items()},
        **{key: path for key, path in rule_paths.items()},
        "documents": documents_path,
        "queries": queries_path,
    }
    sample_manifest = {
        "kind": "sample_manifest",
        "seed": seed,
        "document_count": len(documents),
        "query_count": len(queries),
        "languages": sorted(templates),
        "files": {
            key: {
                "path": path.name,
                "sha256": sha256_bytes(path.read_bytes()),
                "bytes": path.stat().st_size,
            }
            for key, path in sorted(sample_files.items())
        },
    }
    atomic_write_bytes(
        root_path / "sample_manifest.json",
        stable_json_bytes(sample_manifest),
    )
    return sample_manifest


def _query(
    query_id: str,
    query_lang: str,
    terms: list[tuple[str, int]],
    top_k: int,
) -> dict[str, Any]:
    return {
        "query_id": query_id,
        "query_lang": query_lang,
        "terms": [
            {"term": term, "freq": frequency}
            for term, frequency in sorted(terms)
        ],
        "top_k": top_k,
    }


def _bridge_v1() -> BridgeResource:
    pairs = [
        LanguagePair("zh", "en"),
        LanguagePair("zh", "fr"),
        LanguagePair("en", "fr"),
    ]
    entries = _entries(
        [
            ("zh", "en", "汽车", "car", 0.96),
            ("zh", "en", "汽车", "vehicle", 0.71),
            ("zh", "en", "电动车", "electric", 0.84),
            ("zh", "en", "电池", "battery", 0.86),
            ("zh", "en", "车辆", "vehicle", 0.77),
            ("zh", "en", "自行车", "bicycle", 0.88),
            ("zh", "en", "智能", "smart", 0.83),
            ("zh", "en", "卡车", "truck", 0.87),
            ("zh", "fr", "汽车", "voiture", 0.70),
            ("zh", "fr", "电动车", "électrique", 0.80),
            ("zh", "fr", "电池", "batterie", 0.82),
            ("zh", "fr", "车辆", "véhicule", 0.75),
            ("zh", "fr", "卡车", "camion", 0.79),
            ("en", "fr", "car", "voiture", 0.89),
            ("en", "fr", "vehicle", "véhicule", 0.90),
            ("en", "fr", "city", "ville", 0.72),
            ("en", "fr", "bicycle", "vélo", 0.80),
            ("en", "fr", "engine", "moteur", 0.84),
            ("en", "fr", "truck", "camion", 0.86),
        ]
    )
    return _make_bridge(1, pairs, entries)


def _bridge_v2() -> BridgeResource:
    base = _bridge_v1()
    replacements = {
        ("zh", "en", "汽车", "car"): 0.90,
        ("zh", "fr", "汽车", "voiture"): 0.86,
    }
    entries = [
        BridgeEntry(
            source_lang=entry.source_lang,
            target_lang=entry.target_lang,
            source_term=entry.source_term,
            target_term=entry.target_term,
            confidence=replacements.get(entry.key, entry.confidence),
        )
        for entry in base.entries
    ]
    entries.extend(
        _entries(
            [
                ("zh", "en", "汽车", "auto", 0.66),
                ("zh", "en", "电动车", "EV", 0.62),
            ]
        )
    )
    return _make_bridge(2, list(base.language_pairs), entries)


def _bridge_v3() -> BridgeResource:
    base = _bridge_v2()
    replacements = {
        ("zh", "fr", "电动车", "électrique"): 0.75,
    }
    entries = [
        BridgeEntry(
            source_lang=entry.source_lang,
            target_lang=entry.target_lang,
            source_term=entry.source_term,
            target_term=entry.target_term,
            confidence=replacements.get(entry.key, entry.confidence),
        )
        for entry in base.entries
    ]
    entries.extend(_entries([("zh", "fr", "自行车", "vélo", 0.69)]))
    return _make_bridge(3, list(base.language_pairs), entries)


def _make_bridge(
    version: int,
    pairs: list[LanguagePair],
    entries: list[BridgeEntry],
) -> BridgeResource:
    unique = {entry.key: entry for entry in entries}
    ordered_entries = tuple(unique[key] for key in sorted(unique))
    provisional = BridgeResource(
        version=version,
        format_version="bridge-v1",
        language_pairs=tuple(sorted(pairs, key=lambda item: (item.source_lang, item.target_lang))),
        entries=ordered_entries,
        checksum="",
    )
    from .bridge import compute_bridge_checksum

    return BridgeResource(
        version=provisional.version,
        format_version=provisional.format_version,
        language_pairs=provisional.language_pairs,
        entries=provisional.entries,
        checksum=compute_bridge_checksum(provisional),
    )


def _entries(
    rows: list[tuple[str, str, str, str, float]],
) -> list[BridgeEntry]:
    return [
        BridgeEntry(
            source_lang=source_lang,
            target_lang=target_lang,
            source_term=source_term,
            target_term=target_term,
            confidence=confidence,
        )
        for source_lang, target_lang, source_term, target_term, confidence in rows
    ]


def _base_v1_rules() -> list[RewriteRule]:
    rows = [
        ("r_car_zh_en", "zh", "汽车", "en", "car"),
        ("r_car_zh_fr", "zh", "汽车", "fr", "voiture"),
        ("r_electric_zh_en", "zh", "电动车", "en", "electric"),
        ("r_car_en_fr", "en", "car", "fr", "voiture"),
        ("r_vehicle_en_fr", "en", "vehicle", "fr", "véhicule"),
        ("r_bicycle_zh_en", "zh", "自行车", "en", "bicycle"),
        ("r_city_en_fr", "en", "city", "fr", "ville"),
        ("r_smart_zh_en", "zh", "智能", "en", "smart"),
    ]
    return [
        RewriteRule(
            rule_id=rule_id,
            source_lang=source_lang,
            source_term=source_term,
            targets=(Target(target_lang, target_term),),
        )
        for rule_id, source_lang, source_term, target_lang, target_term in rows
    ]


def _rules_v1() -> RuleSet:
    return RuleSet(format_version="v1", rules=tuple(_base_v1_rules()))


def _rules_v2() -> RuleSet:
    weights = {
        "r_car_zh_en": 0.95,
        "r_car_zh_fr": 0.90,
        "r_electric_zh_en": 0.85,
    }
    rules: list[RewriteRule] = []
    for rule in _base_v1_rules():
        rules.append(
            RewriteRule(
                rule_id=rule.rule_id,
                source_lang=rule.source_lang,
                source_term=rule.source_term,
                targets=rule.targets,
                bridge_confidence_weight=weights.get(rule.rule_id, 0.80),
                max_expansion_depth=2 if rule.source_lang == "zh" else 1,
                enabled=True,
                condition_terms=("汽车",) if rule.rule_id == "r_car_zh_fr" else (),
            )
        )
    return RuleSet(format_version="v2", rules=tuple(rules))


def _rules_v3() -> RuleSet:
    car_condition = Condition(
        op="all",
        children=(
            Condition(op="term_present", term="汽车"),
            Condition(
                op="any",
                children=(
                    Condition(op="term_present", term="电动车"),
                    Condition(op="min_freq", term="汽车", value=1),
                ),
            ),
        ),
    )
    truck_condition = Condition(
        op="all",
        children=(
            Condition(op="term_present", term="卡车"),
            Condition(op="min_freq", term="卡车", value=1),
        ),
    )
    rules = [
        RewriteRule(
            rule_id="r_car_zh",
            source_lang="zh",
            source_term="汽车",
            targets=(Target("en", "car"), Target("fr", "voiture")),
            condition=car_condition,
            bridge_confidence_weight=0.95,
            max_expansion_depth=2,
            enabled=True,
        ),
        RewriteRule(
            rule_id="r_electric_zh",
            source_lang="zh",
            source_term="电动车",
            targets=(Target("en", "electric"), Target("fr", "électrique")),
            bridge_confidence_weight=0.85,
            max_expansion_depth=2,
            enabled=True,
        ),
        RewriteRule(
            rule_id="r_truck_zh",
            source_lang="zh",
            source_term="卡车",
            targets=(Target("en", "truck"), Target("fr", "camion")),
            condition=truck_condition,
            bridge_confidence_weight=0.82,
            max_expansion_depth=2,
            enabled=True,
        ),
        RewriteRule(
            rule_id="r_car_en_fr",
            source_lang="en",
            source_term="car",
            targets=(Target("fr", "voiture"),),
            bridge_confidence_weight=0.90,
            max_expansion_depth=2,
            enabled=True,
        ),
        RewriteRule(
            rule_id="r_vehicle_en_fr",
            source_lang="en",
            source_term="vehicle",
            targets=(Target("fr", "véhicule"),),
            bridge_confidence_weight=0.80,
            max_expansion_depth=1,
            enabled=True,
        ),
        RewriteRule(
            rule_id="r_bicycle_zh_en",
            source_lang="zh",
            source_term="自行车",
            targets=(Target("en", "bicycle"),),
            bridge_confidence_weight=0.80,
            max_expansion_depth=1,
            enabled=True,
        ),
        RewriteRule(
            rule_id="r_city_en_fr",
            source_lang="en",
            source_term="city",
            targets=(Target("fr", "ville"),),
            bridge_confidence_weight=0.80,
            max_expansion_depth=1,
            enabled=True,
        ),
        RewriteRule(
            rule_id="r_smart_zh_en",
            source_lang="zh",
            source_term="智能",
            targets=(Target("en", "smart"),),
            bridge_confidence_weight=0.80,
            max_expansion_depth=1,
            enabled=True,
        ),
    ]
    return RuleSet(format_version="v3", rules=tuple(rules))
