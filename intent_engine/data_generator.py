"""Generate the deterministic local demonstration data set."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Iterable

from .resources import EntityEntry
from .sequence import LABEL_ORDER, position_feature_buckets
from .utils import atomic_write_json, atomic_write_text, hash_bucket, quantize, stable_hash


INTENTS = [
    {
        "name": "查询订单",
        "features": [
            {"term": "查询", "weight": 1.4},
            {"term": "订单", "weight": 2.4},
            {"term": "物流", "weight": 2.0},
            {"term": "单号", "weight": 1.6},
            {"term": "到哪", "weight": 1.5},
        ],
        "allowed_entity_types": ["品牌", "数量"],
    },
    {
        "name": "申请退款",
        "features": [
            {"term": "退款", "weight": 2.8},
            {"term": "退货", "weight": 2.2},
            {"term": "不想要", "weight": 1.8},
            {"term": "售后", "weight": 1.2},
        ],
        "allowed_entity_types": ["品牌", "数量", "时间"],
    },
    {
        "name": "修改地址",
        "features": [
            {"term": "修改地址", "weight": 3.0},
            {"term": "地址", "weight": 2.0},
            {"term": "收货", "weight": 1.2},
        ],
        "allowed_entity_types": ["地点", "品牌"],
    },
    {
        "name": "预约安装",
        "features": [
            {"term": "预约", "weight": 2.2},
            {"term": "安装", "weight": 2.6},
            {"term": "上门", "weight": 1.6},
            {"term": "师傅", "weight": 1.3},
        ],
        "allowed_entity_types": ["品牌", "地点", "时间"],
    },
    {
        "name": "投诉服务",
        "features": [
            {"term": "投诉", "weight": 2.8},
            {"term": "态度", "weight": 1.9},
            {"term": "客服", "weight": 1.4},
            {"term": "差评", "weight": 1.8},
        ],
        "allowed_entity_types": ["品牌", "地点"],
    },
    {
        "name": "查询库存",
        "features": [
            {"term": "库存", "weight": 2.9},
            {"term": "现货", "weight": 2.2},
            {"term": "有货", "weight": 2.0},
            {"term": "补货", "weight": 1.6},
        ],
        "allowed_entity_types": ["品牌", "数量", "地点"],
    },
]

ENTITIES = {
    "品牌": [
        {"term": "华为", "priority": 10, "weight": 1.0},
        {"term": "小米手机", "priority": 20, "weight": 1.0},
        {"term": "小米", "priority": 30, "weight": 0.95},
        {"term": "苹果手机", "priority": 15, "weight": 1.0},
        {"term": "苹果", "priority": 40, "weight": 0.9},
        {"term": "荣耀", "priority": 10, "weight": 1.0},
        {"term": "联想", "priority": 10, "weight": 1.0},
    ],
    "地点": [
        {"term": "北京市", "priority": 10, "weight": 1.0},
        {"term": "北京", "priority": 30, "weight": 0.9},
        {"term": "上海", "priority": 10, "weight": 1.0},
        {"term": "深圳", "priority": 10, "weight": 1.0},
        {"term": "杭州", "priority": 10, "weight": 1.0},
        {"term": "广州", "priority": 10, "weight": 1.0},
        {"term": "南京", "priority": 10, "weight": 1.0},
        {"term": "朝阳区", "priority": 20, "weight": 0.9},
    ],
    "数量": [
        {"term": "一台", "priority": 10, "weight": 1.0},
        {"term": "两台", "priority": 10, "weight": 1.0},
        {"term": "三件", "priority": 10, "weight": 1.0},
        {"term": "五个", "priority": 10, "weight": 1.0},
        {"term": "十箱", "priority": 10, "weight": 1.0},
        {"term": "2台", "priority": 10, "weight": 1.0},
        {"term": "3件", "priority": 10, "weight": 1.0},
    ],
    "时间": [
        {"term": "明天上午", "priority": 10, "weight": 1.0},
        {"term": "后天下午", "priority": 10, "weight": 1.0},
        {"term": "明天九点", "priority": 10, "weight": 1.0},
        {"term": "明天", "priority": 30, "weight": 0.85},
        {"term": "昨天", "priority": 10, "weight": 1.0},
        {"term": "上午", "priority": 40, "weight": 0.8},
        {"term": "下午", "priority": 40, "weight": 0.8},
        {"term": "九点", "priority": 40, "weight": 0.8},
        {"term": "今天", "priority": 10, "weight": 1.0},
        {"term": "后天", "priority": 30, "weight": 0.85},
        {"term": "本周六", "priority": 10, "weight": 1.0},
        {"term": "下周一", "priority": 10, "weight": 1.0},
    ],
}

RAW_SAMPLES = [
    ("C001", "我想查询华为订单的物流到哪了", "查询订单", [("品牌", "华为")]),
    ("C002", "麻烦帮我申请小米手机的退款", "申请退款", [("品牌", "小米手机")]),
    ("C003", "把收货地址改到北京市朝阳区可以吗", "修改地址", [("地点", "北京市"), ("地点", "朝阳区")]),
    ("C004", "预约荣耀空调明天上午上门安装", "预约安装", [("品牌", "荣耀"), ("时间", "明天上午")]),
    ("C005", "我要投诉联想客服的服务态度", "投诉服务", [("品牌", "联想")]),
    ("C006", "深圳仓的小米库存还有三件吗", "查询库存", [("地点", "深圳"), ("品牌", "小米"), ("数量", "三件")]),
    ("C007", "苹果手机昨天买的能退货吗", "申请退款", [("品牌", "苹果手机"), ("时间", "昨天")]),
    ("C008", "后天下午来杭州换地址", "修改地址", [("时间", "后天下午"), ("地点", "杭州")]),
    ("C009", "华为有现货吗我想买两台", "查询库存", [("品牌", "华为"), ("数量", "两台")]),
    ("C010", "上海客服态度太差了我要差评", "投诉服务", [("地点", "上海")]),
    ("C011", "预约明天九点在北京安装小米净水器", "预约安装", [("时间", "明天九点"), ("地点", "北京"), ("品牌", "小米")]),
    ("C012", "查一下订单单号物流", "查询订单", []),
    ("C013", "查北京订单物流", "查询订单", [("地点", "北京")]),
    ("C014", "我要退钱不想要了", "申请退款", []),
    ("C015", "苹果手机和小米手机哪个有库存", "查询库存", [("品牌", "苹果手机"), ("品牌", "小米手机")]),
]

SYNONYMS = [
    {"source": "退钱", "target": "退款"},
    {"source": "换地址", "target": "修改地址"},
    {"source": "改地址", "target": "修改地址"},
    {"source": "查订单", "target": "查询订单"},
]

CONFLICTS = [
    {"trigger": "退货退款", "winner": "申请退款", "loser": "查询订单", "penalty": 1.0, "reason": "退货优先退款"},
    {"trigger": "安装地址", "winner": "预约安装", "loser": "修改地址", "penalty": 0.8, "reason": "安装场景优先"},
    {"trigger": "库存退款", "winner": "查询库存", "loser": "申请退款", "penalty": 0.6, "reason": "库存咨询优先"},
]


def _span(text: str, entity_type: str, term: str) -> dict[str, Any]:
    start = text.find(term)
    if start < 0:
        raise ValueError(f"{term!r} not found in {text!r}")
    end = start + len(term)
    return {"type": entity_type, "start": start, "end": end, "text": term}


def build_corpus(seed: int) -> list[dict[str, Any]]:
    samples = []
    for sample_id, text, intent, spans in RAW_SAMPLES:
        samples.append(
            {
                "sample_id": sample_id,
                "text": text,
                "chars": list(text),
                "intent": intent,
                "spans": [_span(text, entity_type, term) for entity_type, term in spans],
                "metadata": {"source": f"synthetic-seed-{seed}"},
            }
        )
    random.Random(seed).shuffle(samples)
    return samples


def _target_labels(text: str, spans: list[dict[str, Any]]) -> list[str]:
    labels = ["O"] * len(text)
    for span in spans:
        start = int(span["start"])
        end = int(span["end"])
        if end - start == 1:
            labels[start] = "S"
        else:
            labels[start] = "B"
            for position in range(start + 1, end - 1):
                labels[position] = "I"
            labels[end - 1] = "E"
    return labels


def _entity_entries() -> list[EntityEntry]:
    result: list[EntityEntry] = []
    for entity_type, entries in ENTITIES.items():
        for item in entries:
            result.append(
                EntityEntry(
                    entity_type=entity_type,
                    term=item["term"],
                    priority=item["priority"],
                    weight=item["weight"],
                    aliases=(),
                    raw=dict(item),
                )
            )
    return result


def train_sequence_model(samples: Iterable[dict[str, Any]], seed: int) -> dict[str, Any]:
    buckets = 257
    feature_dim = 8
    all_chars = sorted({char for sample in samples for char in sample["text"]})
    char_hashes = {char: hash_bucket(f"char:{char}", buckets) for char in all_chars}
    provisional_model = {
        "feature_dim": feature_dim,
        "hash_buckets": buckets,
        "char_embedding_hashes": char_hashes,
        "labels": list(LABEL_ORDER),
    }
    entries = _entity_entries()
    emission: dict[str, dict[str, float]] = {label: {} for label in LABEL_ORDER}
    training_rows = 0
    structural_names = {"hitlen", "hitstart", "hitend", "hittype"}
    for sample in samples:
        chars = list(sample["text"])
        targets = _target_labels(sample["text"], sample["spans"])
        for position, target in enumerate(targets):
            training_rows += 1
            features = position_feature_buckets(chars, position, provisional_model, entries)
            for label in LABEL_ORDER:
                for feature_name, bucket in features:
                    structural = feature_name in structural_names
                    if target != "O":
                        delta = 7.0 if label == target and structural else (1.4 if label == target else -0.35)
                    else:
                        delta = 2.0 if label == target and structural else (1.0 if label == target else -0.08)
                    key = str(bucket)
                    emission[label][key] = emission[label].get(key, 0.0) + delta
    # Seeded micro-jitter proves that generation is reproducible without
    # affecting the structural weights.  It is written as rounded floats.
    rng = random.Random(seed + 7919)
    for label in LABEL_ORDER:
        for bucket in sorted(emission[label], key=lambda value: int(value)):
            emission[label][bucket] = quantize(
                emission[label][bucket] + rng.uniform(-0.015, 0.015), 6
            )
    transition_weights = {
        "B": {"I": 1.4, "E": 1.2},
        "I": {"I": 1.1, "E": 1.5},
        "E": {"B": 0.8, "S": 0.9, "O": 0.7},
        "S": {"B": 0.7, "S": 0.5, "O": 0.6},
        "O": {"B": 1.0, "S": 1.0, "O": 0.8},
    }
    return {
        "schema_version": 1,
        "labels": list(LABEL_ORDER),
        "feature_dim": feature_dim,
        "hash_buckets": buckets,
        "char_embedding_hashes": char_hashes,
        "emission_weights": {
            label: {key: emission[label][key] for key in sorted(emission[label], key=lambda value: int(value))}
            for label in LABEL_ORDER
        },
        "transition_weights": transition_weights,
        "start_weights": {"B": 0.5, "I": -3.0, "E": -3.0, "S": 0.9, "O": 1.0},
        "end_weights": {"B": -3.0, "I": -3.0, "E": 1.1, "S": 1.0, "O": 1.0},
        "training": {
            "seed": seed,
            "rows": training_rows,
            "feature_scheme": ["bias", "cur", "prev", "next", "hitlen", "hitstart", "hitend", "hittype"],
            "method": "deterministic_target_feature_vote",
        },
    }


def engine_config() -> dict[str, Any]:
    return {
        "version": 1,
        "concurrency": 4,
        "max_edit_batch_size": 8,
        "confidence_threshold": 0.55,
        "precision": "high",
        "snapshot_commit_interval": 3,
        "graceful_exit_wait_ms": 2000,
        "normalize": {
            "unicode_form": "NFKC",
            "collapse_whitespace": True,
            "strip": True,
            "apply_synonyms": True,
        },
        "scoring": {
            "length_bonus": 0.15,
            "position_decay": 0.02,
            "default_conflict_penalty": 0.75,
        },
        "slots": {
            "require_intent_compatibility": True,
            "incompatible_penalty": 0.5,
            "source_priority": ["dictionary", "sequence"],
        },
        "merge": {
            "source_priority": ["dictionary", "sequence"],
            "deduplicate": True,
        },
        "sequence": {"round_path_score": True},
    }


def injection_config() -> dict[str, Any]:
    return {
        "version": 1,
        "description": "Deterministic real-input faults used by the demonstration and pytest suite.",
        "injections": [
            {
                "id": "INJ-EMPTY",
                "sample_id": "C001",
                "stage": "normalize",
                "fault": "empty_sentence",
            },
            {
                "id": "INJ-CHARS",
                "sample_id": "C002",
                "stage": "normalize",
                "fault": "missing_chars",
            },
            {
                "id": "INJ-ENTITY",
                "sample_id": "C003",
                "stage": "slots",
                "fault": "remove_entity",
                "term": "北京市",
            },
            {
                "id": "INJ-CONFLICT",
                "sample_id": "C007",
                "stage": "intent",
                "fault": "rule_conflict_pair",
                "trigger": "退款",
                "left": "查询订单",
                "right": "申请退款",
                "penalty": 1.8,
            },
            {
                "id": "INJ-THRESHOLD",
                "sample_id": "C010",
                "stage": "intent",
                "fault": "confidence_threshold",
            },
            {
                "id": "INJ-NONFINITE",
                "sample_id": "C006",
                "stage": "sequence",
                "fault": "nonfinite_weight",
                "label": "B",
            },
        ],
    }


def invalid_corpus() -> list[Any]:
    valid = {
        "sample_id": "BAD-OK",
        "text": "查询华为订单物流",
        "intent": "查询订单",
        "spans": [_span("查询华为订单物流", "品牌", "华为")],
    }
    return [
        {"sample_id": "BAD-EMPTY", "text": "   ", "intent": "查询订单"},
        "__INVALID_JSON__",
        {"sample_id": "BAD-INTENT", "text": "查询未知事项", "intent": "不存在意图"},
        {
            "sample_id": "BAD-SPAN",
            "text": "查询华为订单",
            "intent": "查询订单",
            "spans": [{"type": "品牌", "start": 2, "end": 99, "text": "华为订单"}],
        },
        {"sample_id": "C001", "text": "重复样本", "intent": "查询订单"},
        {"sample_id": "C001", "text": "另一个重复样本", "intent": "查询订单"},
        valid,
    ]


def generate_dataset(out_dir: str | Path, seed: int = 42) -> dict[str, Any]:
    root = Path(out_dir)
    resources = root / "resources"
    resources.mkdir(parents=True, exist_ok=True)
    samples = build_corpus(seed)
    intents_doc = {
        "version": 1,
        "description": "客服与工单单句意图词表",
        "intents": INTENTS,
    }
    entities_doc = {
        "version": 1,
        "priority_rule": "smaller priority wins for equal-length matches",
        "entities": ENTITIES,
    }
    rules_doc = {
        "version": 1,
        "synonyms": SYNONYMS,
        "conflicts": CONFLICTS,
    }
    sequence_model = train_sequence_model(samples, seed)
    config = engine_config()
    injections = injection_config()
    atomic_write_text(root / "corpus.jsonl", "\n".join(json.dumps(sample, ensure_ascii=False) for sample in samples) + "\n")
    invalid_lines = []
    for item in invalid_corpus():
        invalid_lines.append(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
    atomic_write_text(root / "invalid_corpus.jsonl", "\n".join(invalid_lines) + "\n")
    atomic_write_json(resources / "intents.json", intents_doc)
    atomic_write_json(resources / "entities.json", entities_doc)
    atomic_write_json(resources / "sequence_model.json", sequence_model)
    atomic_write_json(resources / "rules.json", rules_doc)
    atomic_write_json(resources / "config.json", config)
    atomic_write_json(root / "inject.json", injections)
    manifest = {
        "seed": seed,
        "sample_count": len(samples),
        "invalid_record_count": len(invalid_lines),
        "files": {
            "corpus": "corpus.jsonl",
            "invalid_corpus": "invalid_corpus.jsonl",
            "injections": "inject.json",
            "resources": [
                "resources/intents.json",
                "resources/entities.json",
                "resources/sequence_model.json",
                "resources/rules.json",
                "resources/config.json",
            ],
        },
        "resource_hash": stable_hash(
            {
                "intents": intents_doc,
                "entities": entities_doc,
                "rules": rules_doc,
                "sequence_model": sequence_model,
            }
        ),
    }
    atomic_write_json(root / "manifest.json", manifest)
    return manifest
