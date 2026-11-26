"""Fixed-seed local fixture and two-generation strategy generator."""

from __future__ import annotations

import argparse
import json
import random
import shutil
from pathlib import Path
from typing import Any

from .config import DEFAULT_CONFIG
from .errors import BusinessError, E_INVALID_INPUT
from .strategy import V1StrategyStore, V2StrategyStore
from .utils import (
    atomic_write_json,
    atomic_write_jsonl,
    canonical_json,
    iso_to_ms,
    sha256_file,
)


DEFAULT_SEED = 20261127
FIXED_TIMESTAMP = "2026-11-27T00:00:00.000Z"
FIXED_TIMESTAMP_MS = iso_to_ms(FIXED_TIMESTAMP, 0)

TEMPLATES: dict[str, Any] = {
    "schema_version": 1,
    "templates": [
        {
            "template_id": "zh-collection",
            "lang": "zh",
            "source_tags": ["wiki", "forum"],
            "instruction_template": "请根据以下任务给出回答：{instruction}",
            "input_template": "参考材料：{input}",
        },
        {
            "template_id": "zh-operations",
            "lang": "zh",
            "source_tags": ["manual", "qa"],
            "instruction_template": "任务说明：{instruction}",
            "input_template": "上下文信息：{input}",
        },
        {
            "template_id": "en-collection",
            "lang": "en",
            "source_tags": ["wiki", "forum"],
            "instruction_template": "Answer the following task: {instruction}",
            "input_template": "Reference material: {input}",
        },
        {
            "template_id": "en-operations",
            "lang": "en",
            "source_tags": ["manual", "qa"],
            "instruction_template": "Task instruction: {instruction}",
            "input_template": "Context: {input}",
        },
    ],
}

CLEANING_RULES: dict[str, Any] = {
    "schema_version": 1,
    "weights": {"length": 0.35, "repetition": 0.35, "noise": 0.30},
    "length": {
        "min_chars": 40,
        "ideal_min": 120,
        "ideal_max": 900,
        "max_chars": 6000,
    },
    "repetition": {"ngram_size": 3, "zero_at_rate": 0.80},
    "noise": {
        "characters": "\u0000\u0001\u0002\u0007\u0008\u000b\u000c\u001f\ufffd",
        "zero_at_rate": 0.15,
    },
    "tiers": {"gold": 0.82, "silver": 0.65},
}

QUOTA_TARGETS: dict[str, Any] = {
    "schema_version": 1,
    "seed": DEFAULT_SEED,
    "total_target": 18,
    "allow_quality_relaxation": True,
    "max_tier_drop": 1,
    "tier_order": ["gold", "silver", "bronze"],
    "targets": [
        {"source_tag": "wiki", "quality_tier": "gold", "quota": 4},
        {"source_tag": "wiki", "quality_tier": "silver", "quota": 2},
        {"source_tag": "wiki", "quality_tier": "bronze", "quota": 0},
        {"source_tag": "forum", "quality_tier": "gold", "quota": 5},
        {"source_tag": "forum", "quality_tier": "silver", "quota": 0},
        {"source_tag": "forum", "quality_tier": "bronze", "quota": 0},
        {"source_tag": "manual", "quality_tier": "gold", "quota": 0},
        {"source_tag": "manual", "quality_tier": "silver", "quota": 4},
        {"source_tag": "manual", "quality_tier": "bronze", "quota": 0},
        {"source_tag": "qa", "quality_tier": "gold", "quota": 4},
        {"source_tag": "qa", "quality_tier": "silver", "quota": 0},
        {"source_tag": "qa", "quality_tier": "bronze", "quota": 0},
    ],
}

ZH_TOPICS = [
    ("缓存", "解释缓存穿透与缓存雪崩的区别，并给出两种防护措施。"),
    ("SQLite", "说明 WAL 模式下读写并发的基本行为。"),
    ("批处理", "给出一个按批处理大文件以控制内存占用的方案。"),
    ("任务恢复", "描述如何从已提交检查点恢复一个中断的数据流水线。"),
    ("配额", "说明分层配额采样中欠配的原因和记录方式。"),
    ("去重", "比较精确去重与近似去重的适用场景。"),
    ("模板", "说明模板占位符校验失败时应如何定位问题。"),
    ("调度", "解释 worker 数量增加时内存压力为何同步增加。"),
    ("校验和", "说明内容校验和如何防止快照被部分覆盖。"),
    ("数据导出", "说明训练数据逐条追溯至少需要保留哪些标识。"),
]

EN_TOPICS = [
    ("cache stampede", "Explain cache penetration and cache stampede with two mitigations."),
    ("sqlite WAL", "Describe read and write concurrency under SQLite WAL mode."),
    ("batching", "Propose a batched file-processing design with bounded memory."),
    ("recovery", "Describe how to resume a pipeline from a committed checkpoint."),
    ("quota", "Explain underfill and audit records in stratified quota sampling."),
    ("dedup", "Compare exact deduplication with approximate near-duplicate detection."),
    ("template", "Explain how to diagnose a missing template placeholder."),
    ("scheduler", "Explain why more workers can increase resident memory pressure."),
    ("checksum", "Explain how checksums prevent partially replaced snapshots."),
    ("export", "List identifiers needed for row-level training data provenance."),
]


def _quality_shape(index: int, lang: str) -> tuple[str, str, str]:
    zh = [
        "先说结论，再给出可执行的步骤，并说明验证方式。",
        "需要同时考虑正确性、可维护性和失败恢复。",
        "请给出最小实现，同时指出边界条件。",
        "回答应包含原因、步骤和至少一个反例。",
    ]
    en = [
        "State the conclusion first, then give executable steps and validation.",
        "Consider correctness, maintainability and failure recovery together.",
        "Provide a minimal implementation and identify boundary conditions.",
        "Include rationale, steps and at least one counterexample.",
    ]
    variant = index % 8
    if variant == 0:
        padding = (
            "需要覆盖输入校验、错误码、原子提交、恢复路径与审计记录，"
            "并对每个关键决策保留可复现的种子和预算值。"
            if lang == "zh"
            else "Cover validation, error codes, atomic commit, recovery and audit records, "
            "while preserving reproducible seeds and budget values for key decisions."
        )
        return "gold", padding, "high"
    if variant in (1, 2, 3):
        return "silver", (zh if lang == "zh" else en)[index % 4], "medium"
    if variant == 4:
        return "bronze", ("重复内容。" * 12 if lang == "zh" else "repeat. " * 16), "low"
    if variant == 5:
        return "bronze", "短答。", "low"
    if variant == 6:
        return "silver", "\u0007" * 3 + ("噪声字符用于验证清洗评分。" if lang == "zh" else "noise chars test cleaning."), "noisy"
    return "gold", (zh if lang == "zh" else en)[index % 4], "medium"


def _make_raw_samples(seed: int, count: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    source_tags = list(DEFAULT_CONFIG["source_tags"])
    languages = list(DEFAULT_CONFIG["languages"])
    rows: list[dict[str, Any]] = []
    base_count = max(1, count // 2)
    for index in range(base_count):
        lang = languages[index % len(languages)]
        source = source_tags[(index + index // len(languages)) % len(source_tags)]
        topics = ZH_TOPICS if lang == "zh" else EN_TOPICS
        topic, instruction = topics[index % len(topics)]
        if lang == "zh":
            input_text = (
                f"背景：{topic} 场景来自本地固定样本，编号 {index:03d}。"
                f"要求在不访问外部网络的前提下完成说明。"
            )
        else:
            input_text = (
                f"Context: the {topic} case is local fixture {index:03d}. "
                "The answer must not depend on external network access."
            )
        quality_flag, output, _ = _quality_shape(index, lang)
        rows.append(
            {
                "sample_id": f"s{len(rows) + 1:04d}",
                "instruction": instruction,
                "input": input_text,
                "output": output,
                "source_tag": source,
                "quality_flag": quality_flag,
                "lang": lang,
                "annotation_revision": (
                    ["1.0.0", "r12", "rev-7", "3.1.4"][index % 4]
                ),
            }
        )
        if index % 4 == 1 and len(rows) < count:
            duplicate = dict(rows[-1])
            duplicate["sample_id"] = f"s{len(rows) + 1:04d}"
            duplicate["input"] = (
                input_text
                + (" 补充：请保持回答简洁。" if lang == "zh" else " Keep it concise.")
            )
            duplicate["output"] = output
            duplicate["annotation_revision"] = "r13"
            rows.append(duplicate)
    while len(rows) < count:
        index = len(rows)
        lang = languages[index % len(languages)]
        source = source_tags[index % len(source_tags)]
        topics = ZH_TOPICS if lang == "zh" else EN_TOPICS
        topic, instruction = topics[index % len(topics)]
        quality_flag, output, _ = _quality_shape(index + 3, lang)
        rows.append(
            {
                "sample_id": f"s{len(rows) + 1:04d}",
                "instruction": instruction,
                "input": (
                    f"补充背景：{topic}，本地编号 {index:03d}。"
                    if lang == "zh"
                    else f"Additional context: {topic}, local id {index:03d}."
                ),
                "output": output,
                "source_tag": source,
                "quality_flag": quality_flag,
                "lang": lang,
                "annotation_revision": f"rev-{index + 20}",
            }
        )
    rng.shuffle(rows)
    return rows


def _strategy_config() -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG, ensure_ascii=False))
    config["quality"] = json.loads(
        json.dumps(CLEANING_RULES, ensure_ascii=False)
    )
    sampling = {
        key: value
        for key, value in QUOTA_TARGETS.items()
        if key not in {"schema_version", "targets"}
    }
    config["sampling"] = sampling
    return config


def _v1_snapshot() -> dict[str, Any]:
    from .strategy import strategy_checksum

    snapshot = {
        "version": 1,
        "strategy_id": "local-deterministic",
        "revision": 1,
        "snapshot_version": 1,
        "created_at": FIXED_TIMESTAMP,
        "updated_at": FIXED_TIMESTAMP,
        "priorities": [
            "progress",
            "memory",
            "sampling_target",
            "dedup_quality",
        ],
        "retired_keys": ["legacy_single_window_dedup"],
        "config": _strategy_config(),
    }
    snapshot["checksum"] = strategy_checksum(snapshot)
    return snapshot


def _v2_snapshot() -> dict[str, Any]:
    from .strategy import strategy_checksum

    snapshot = {
        "schema_version": 2,
        "strategy_generation": 2,
        "strategy_id": "local-deterministic",
        "revision": 2,
        "snapshot_version": 2,
        "created_at_ms": FIXED_TIMESTAMP_MS,
        "updated_at_ms": FIXED_TIMESTAMP_MS,
        "priorities": [
            "progress",
            "memory",
            "sampling_target",
            "dedup_quality",
        ],
        "tombstones": [
            {
                "key": "config.dedup.single_window",
                "reason": "replaced by candidate_windows budget arbitration",
                "since_ms": FIXED_TIMESTAMP_MS,
                "source": "v2-generation",
            }
        ],
        "source": {
            "kind": "fixed-seed-generator",
            "seed": DEFAULT_SEED,
            "completed_by": "ifdata.generator._v2_snapshot",
        },
        "config": _strategy_config(),
    }
    snapshot["checksum"] = strategy_checksum(snapshot)
    return snapshot


def generate_fixture(
    out_dir: str | Path,
    *,
    seed: int = DEFAULT_SEED,
    count: int = 48,
    force: bool = False,
) -> dict[str, Any]:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    raw_path = root / "raw_samples.jsonl"
    if raw_path.exists() and not force:
        existing_manifest_path = root / "generation_manifest.json"
        idempotent = False
        if existing_manifest_path.exists():
            existing = json.loads(
                existing_manifest_path.read_text(encoding="utf-8")
            )
            idempotent = (
                int(existing.get("seed", -1)) == seed
                and int(existing.get("sample_rows", -1)) == count
            )
        if not idempotent:
            raise BusinessError(
                E_INVALID_INPUT,
                (
                    "output contains a different generated fixture; "
                    "pass force=True to replace"
                ),
                details={"path": str(raw_path)},
            )
    rows = _make_raw_samples(seed, count)
    atomic_write_jsonl(raw_path, rows)
    atomic_write_json(root / "templates.json", TEMPLATES)
    atomic_write_json(root / "cleaning_rules.json", CLEANING_RULES)
    atomic_write_json(root / "quota_targets.json", QUOTA_TARGETS)
    strategy_root = root / "strategy"
    if strategy_root.exists() and force:
        shutil.rmtree(strategy_root)
    v1_path = strategy_root / "v1" / "strategy.json"
    v1_store = V1StrategyStore(v1_path)
    v1_snapshot = _v1_snapshot()
    v1_store.commit(
        v1_snapshot,
        owner=f"generator-{seed}",
        expected_version=(
            v1_snapshot["snapshot_version"]
            if not v1_path.exists()
            else None
        ),
    )
    v2_root = strategy_root / "v2"
    v2_store = V2StrategyStore(v2_root)
    v2_snapshot = _v2_snapshot()
    v2_active = v2_store.commit(
        v2_snapshot,
        owner=f"generator-{seed}",
    )
    manifest = {
        "schema_version": 1,
        "seed": seed,
        "sample_rows": len(rows),
        "fixed_timestamp_ms": FIXED_TIMESTAMP_MS,
        "files": {
            name: {
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
            for name, path in {
                "raw_samples.jsonl": raw_path,
                "templates.json": root / "templates.json",
                "cleaning_rules.json": root / "cleaning_rules.json",
                "quota_targets.json": root / "quota_targets.json",
                "strategy.v1.json": v1_path,
                "strategy.v2.manifest.json": (
                    v2_root / "manifest.json"
                ),
            }.items()
            if path.exists()
        },
        "strategy": {
            "v1": {
                "path": str(v1_path),
                "snapshot_version": v1_store.load()["snapshot_version"],
                    "checksum": v1_store.load()["checksum"],
            },
            "v2": {
                "path": str(v2_root),
                "snapshot_version": v2_active["snapshot_version"],
                "checksum": v2_active["checksum"],
                "generation_dir": v2_active.get("_generation_dir"),
            },
        },
    }
    atomic_write_json(root / "generation_manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate deterministic local instruction-tuning fixtures"
    )
    parser.add_argument("--out", default="data", help="output directory")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--count", type=int, default=48)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = generate_fixture(
        args.out,
        seed=args.seed,
        count=args.count,
        force=args.force,
    )
    print(canonical_json(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

