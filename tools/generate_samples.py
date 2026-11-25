#!/usr/bin/env python3
"""Generate the local, fixed-seed corpus used by the prototype.

The generator uses a fixed seed only to permute the predefined templates.
All content, term definitions, feature ranges, and notes are deterministic.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any


SEED = 20261126
SHARD_COUNT = 4
DOCS_PER_SHARD = 8


GLOSSARY: list[dict[str, Any]] = [
    {
        "entry_no": 1,
        "term_id": "T_NET_PROTOCOL",
        "term": "网络协议",
        "source": "domain_core",
        "source_weight": 1.00,
        "length_tier": "L",
        "rule_version": "glossary-v1",
        "senses": [{"sense_id": 1, "definition": "计算机网络通信协议"}],
    },
    {
        "entry_no": 2,
        "term_id": "T_PROTOCOL",
        "term": "协议",
        "source": "domain_core",
        "source_weight": 1.00,
        "length_tier": "M",
        "rule_version": "glossary-v1",
        "senses": [
            {"sense_id": 1, "definition": "网络通信协议"},
            {"sense_id": 2, "definition": "协议文本或业务约定"},
        ],
    },
    {
        "entry_no": 3,
        "term_id": "T_PACKET",
        "term": "数据包",
        "source": "domain_core",
        "source_weight": 1.10,
        "length_tier": "L",
        "rule_version": "glossary-v1",
        "senses": [{"sense_id": 1, "definition": "网络传输数据单元"}],
    },
    {
        "entry_no": 4,
        "term_id": "T_BUS",
        "term": "总线",
        "source": "domain_core",
        "source_weight": 0.95,
        "length_tier": "M",
        "rule_version": "glossary-v1",
        "senses": [
            {"sense_id": 1, "definition": "计算机内部总线"},
            {"sense_id": 2, "definition": "电力或现场总线"},
        ],
    },
    {
        "entry_no": 5,
        "term_id": "T_CACHE",
        "term": "缓存",
        "source": "domain_core",
        "source_weight": 1.05,
        "length_tier": "M",
        "rule_version": "glossary-v1",
        "senses": [
            {"sense_id": 1, "definition": "处理器高速缓存"},
            {"sense_id": 2, "definition": "操作系统页缓存"},
        ],
    },
    {
        "entry_no": 6,
        "term_id": "T_KERNEL",
        "term": "内核",
        "source": "domain_core",
        "source_weight": 1.00,
        "length_tier": "M",
        "rule_version": "glossary-v1",
        "senses": [{"sense_id": 1, "definition": "操作系统内核"}],
    },
    {
        "entry_no": 7,
        "term_id": "T_MEMORY",
        "term": "内存",
        "source": "domain_core",
        "source_weight": 1.05,
        "length_tier": "M",
        "rule_version": "glossary-v1",
        "senses": [
            {"sense_id": 1, "definition": "物理内存硬件"},
            {"sense_id": 2, "definition": "虚拟内存地址空间"},
        ],
    },
    {
        "entry_no": 8,
        "term_id": "T_PAGE_TABLE",
        "term": "页表",
        "source": "domain_core",
        "source_weight": 1.00,
        "length_tier": "M",
        "rule_version": "glossary-v1",
        "senses": [{"sense_id": 1, "definition": "虚拟地址页表"}],
    },
    {
        "entry_no": 9,
        "term_id": "T_TRANSACTION",
        "term": "事务",
        "source": "domain_core",
        "source_weight": 1.05,
        "length_tier": "M",
        "rule_version": "glossary-v1",
        "senses": [
            {"sense_id": 1, "definition": "数据库事务"},
            {"sense_id": 2, "definition": "业务流程事务"},
        ],
    },
    {
        "entry_no": 10,
        "term_id": "T_PORT",
        "term": "端口",
        "source": "domain_core",
        "source_weight": 0.95,
        "length_tier": "M",
        "rule_version": "glossary-v1",
        "senses": [
            {"sense_id": 1, "definition": "网络传输端口"},
            {"sense_id": 2, "definition": "硬件寄存器端口"},
        ],
    },
    {
        "entry_no": 11,
        "term_id": "T_COMPILER",
        "term": "编译器",
        "source": "domain_core",
        "source_weight": 1.10,
        "length_tier": "L",
        "rule_version": "glossary-v1",
        "senses": [{"sense_id": 1, "definition": "源代码编译器"}],
    },
    {
        "entry_no": 12,
        "term_id": "T_INDEX",
        "term": "索引",
        "source": "domain_core",
        "source_weight": 1.00,
        "length_tier": "M",
        "rule_version": "glossary-v1",
        "senses": [{"sense_id": 1, "definition": "数据索引结构"}],
    },
    {
        "entry_no": 13,
        "term_id": "T_SNAPSHOT",
        "term": "快照",
        "source": "domain_core",
        "source_weight": 1.00,
        "length_tier": "M",
        "rule_version": "glossary-v1",
        "senses": [{"sense_id": 1, "definition": "状态快照"}],
    },
    {
        "entry_no": 20,
        "term_id": "T_MICRO_20",
        "term": "微内核",
        "source": "domain_core",
        "source_weight": 1.15,
        "length_tier": "L",
        "rule_version": "glossary-v1",
        "senses": [{"sense_id": 1, "definition": "微内核操作系统架构"}],
    },
    {
        "entry_no": 21,
        "term_id": "T_MICRO_21",
        "term": "微内核",
        "source": "domain_core",
        "source_weight": 1.15,
        "length_tier": "L",
        "rule_version": "glossary-v1",
        "senses": [{"sense_id": 1, "definition": "微内核操作系统架构（别名条目）"}],
    },
    {
        "entry_no": 22,
        "term_id": "T_SCHEDULER",
        "term": "调度器",
        "source": "domain_core",
        "source_weight": 1.05,
        "length_tier": "L",
        "rule_version": "glossary-v1",
        "senses": [{"sense_id": 1, "definition": "操作系统任务调度器"}],
    },
    {
        "entry_no": 23,
        "term_id": "T_SECURITY_DOMAIN",
        "term": "安全域",
        "source": "domain_core",
        "source_weight": 1.05,
        "length_tier": "L",
        "rule_version": "glossary-v1",
        "senses": [{"sense_id": 1, "definition": "安全隔离域"}],
    },
    {
        "entry_no": 24,
        "term_id": "T_VECTOR",
        "term": "向量",
        "source": "domain_core",
        "source_weight": 0.90,
        "length_tier": "M",
        "rule_version": "glossary-v1",
        "senses": [{"sense_id": 1, "definition": "数学或数据向量"}],
    },
    {
        "entry_no": 25,
        "term_id": "T_PROTOCOL_STACK",
        "term": "协议栈",
        "source": "domain_core",
        "source_weight": 1.15,
        "length_tier": "L",
        "rule_version": "glossary-v1",
        "senses": [{"sense_id": 1, "definition": "分层网络协议栈"}],
    },
]


FEATURE_CATALOG: list[dict[str, Any]] = [
    {"feature_id": "F_NET", "description": "网络通信语境", "weight": 1.20},
    {"feature_id": "F_BIZ_TEXT", "description": "协议文本与业务约定", "weight": 1.15},
    {"feature_id": "F_CPU", "description": "处理器语境", "weight": 1.25},
    {"feature_id": "F_BUS", "description": "计算机总线语境", "weight": 1.05},
    {"feature_id": "F_POWER", "description": "电力总线语境", "weight": 1.10},
    {"feature_id": "F_CACHE_CPU", "description": "处理器缓存语境", "weight": 1.20},
    {"feature_id": "F_PAGE", "description": "页缓存与分页语境", "weight": 1.15},
    {"feature_id": "F_MEM_HW", "description": "物理内存硬件语境", "weight": 1.20},
    {"feature_id": "F_VMEM", "description": "虚拟内存语境", "weight": 1.10},
    {"feature_id": "F_DB", "description": "数据库事务语境", "weight": 1.30},
    {"feature_id": "F_BIZ_TX", "description": "业务流程事务语境", "weight": 1.10},
    {"feature_id": "F_PORT_NET", "description": "网络端口语境", "weight": 1.15},
    {"feature_id": "F_PORT_HW", "description": "硬件端口语境", "weight": 1.00},
    {"feature_id": "F_KERNEL", "description": "内核语境", "weight": 1.00},
    {"feature_id": "F_SECURITY", "description": "安全审计语境", "weight": 1.05},
    {"feature_id": "F_COMPILER", "description": "编译器语境", "weight": 1.00},
]


TEMPLATES: list[dict[str, Any]] = [
    {
        "text": "网络协议要求协议字段在协议栈中保持字节序一致，数据包按序交付。",
        "features": [
            ("F_NET", "网络协议"),
            ("F_NET", "协议栈"),
            ("F_NET", "数据包"),
        ],
    },
    {
        "text": "双方签署的协议文本需要归档，业务条款由安全域审计。",
        "features": [
            ("F_BIZ_TEXT", "协议文本"),
            ("F_SECURITY", "安全域"),
        ],
    },
    {
        "text": "系统总线连接处理器与缓存，缓存行命中率影响调度器吞吐。",
        "features": [
            ("F_BUS", "系统总线"),
            ("F_CPU", "处理器"),
            ("F_CACHE_CPU", "缓存行"),
            ("F_CPU", "调度器"),
        ],
    },
    {
        "text": "电力总线通过保护装置隔离故障，母线电压保持稳定。",
        "features": [("F_POWER", "电力总线")],
    },
    {
        "text": "一级缓存命中率影响内核执行路径，缓存行需要对齐。",
        "features": [
            ("F_CACHE_CPU", "一级缓存"),
            ("F_KERNEL", "内核"),
            ("F_CACHE_CPU", "缓存行"),
        ],
    },
    {
        "text": "页缓存保存文件数据，缺页中断会触发内存回收。",
        "features": [
            ("F_PAGE", "页缓存"),
            ("F_PAGE", "缺页中断"),
            ("F_VMEM", "内存回收"),
        ],
    },
    {
        "text": "内存条容量决定驻留数据规模，内存带宽影响拷贝。",
        "features": [
            ("F_MEM_HW", "内存条"),
            ("F_MEM_HW", "内存带宽"),
        ],
    },
    {
        "text": "虚拟内存通过页表映射地址，内存交换写入磁盘。",
        "features": [
            ("F_VMEM", "虚拟内存"),
            ("F_PAGE", "页表"),
            ("F_VMEM", "内存交换"),
        ],
    },
    {
        "text": "数据库事务保证提交原子性，事务日志用于恢复。",
        "features": [
            ("F_DB", "数据库事务"),
            ("F_DB", "事务日志"),
        ],
    },
    {
        "text": "业务事务跨越多个审批节点，事务状态由工作流记录。",
        "features": [
            ("F_BIZ_TX", "业务事务"),
            ("F_BIZ_TX", "事务状态"),
        ],
    },
    {
        "text": "网络端口监听请求，端口号在数据包头部编码。",
        "features": [
            ("F_PORT_NET", "网络端口"),
            ("F_PORT_NET", "端口号"),
            ("F_NET", "数据包"),
        ],
    },
    {
        "text": "调试端口连接总线控制器，端口寄存器映射到内存。",
        "features": [
            ("F_PORT_HW", "调试端口"),
            ("F_PORT_HW", "端口寄存器"),
            ("F_BUS", "总线控制器"),
            ("F_MEM_HW", "内存"),
        ],
    },
    {
        "text": "编译器生成中间向量，向量索引由调度器维护。",
        "features": [
            ("F_COMPILER", "编译器"),
            ("F_COMPILER", "中间向量"),
            ("F_CPU", "调度器"),
        ],
    },
    {
        "text": "微内核只保留地址空间隔离，快照用于恢复安全域。",
        "features": [
            ("F_KERNEL", "微内核"),
            ("F_SECURITY", "安全域"),
            ("F_SECURITY", "快照"),
        ],
    },
    {
        "text": "协议栈分层处理网络协议，协议字段由校验器验证。",
        "features": [
            ("F_NET", "协议栈"),
            ("F_NET", "网络协议"),
            ("F_NET", "协议字段"),
        ],
    },
    {
        "text": "安全域隔离静态数据，快照版本由审计中心记录。",
        "features": [
            ("F_SECURITY", "安全域"),
            ("F_SECURITY", "快照"),
        ],
    },
    {
        "text": "内核调度器按时间片选择任务，缓存局部性影响延迟。",
        "features": [
            ("F_KERNEL", "内核"),
            ("F_CPU", "调度器"),
            ("F_CACHE_CPU", "缓存"),
        ],
    },
    {
        "text": "协议文本的快照索引保存在安全域，事务审计可追溯。",
        "features": [
            ("F_BIZ_TEXT", "协议文本"),
            ("F_SECURITY", "安全域"),
            ("F_DB", "事务审计"),
        ],
    },
]


def _rules(version: str, scheme: str, scale: float) -> dict[str, Any]:
    base = [
        ("R_PROTOCOL_NET", "T_PROTOCOL", 1, "F_NET", 2.50),
        ("R_PROTOCOL_TEXT", "T_PROTOCOL", 2, "F_BIZ_TEXT", 2.40),
        ("R_BUS_CPU", "T_BUS", 1, "F_BUS", 2.00),
        ("R_BUS_CPU_AUX", "T_BUS", 1, "F_CPU", 0.60),
        ("R_BUS_POWER", "T_BUS", 2, "F_POWER", 2.30),
        ("R_CACHE_CPU", "T_CACHE", 1, "F_CACHE_CPU", 2.40),
        ("R_CACHE_CPU_AUX", "T_CACHE", 1, "F_CPU", 0.55),
        ("R_CACHE_PAGE", "T_CACHE", 2, "F_PAGE", 2.35),
        ("R_MEMORY_HW", "T_MEMORY", 1, "F_MEM_HW", 2.30),
        ("R_MEMORY_VM", "T_MEMORY", 2, "F_VMEM", 2.30),
        ("R_MEMORY_VM_AUX", "T_MEMORY", 2, "F_PAGE", 0.50),
        ("R_TRANSACTION_DB", "T_TRANSACTION", 1, "F_DB", 2.50),
        ("R_TRANSACTION_BIZ", "T_TRANSACTION", 2, "F_BIZ_TX", 2.40),
        ("R_PORT_NET", "T_PORT", 1, "F_PORT_NET", 2.30),
        ("R_PORT_NET_AUX", "T_PORT", 1, "F_NET", 0.45),
        ("R_PORT_HW", "T_PORT", 2, "F_PORT_HW", 2.25),
        ("R_PORT_HW_AUX", "T_PORT", 2, "F_BUS", 0.40),
    ]
    rules = []
    for rule_id, term_id, sense_id, feature_id, weight in base:
        rules.append(
            {
                "rule_id": rule_id,
                "term_id": term_id,
                "sense_id": sense_id,
                "feature_id": feature_id,
                "weight": round(weight * scale, 6),
            }
        )
    return {
        "rule_version": version,
        "scheme": scheme,
        "glossary_version": "glossary-v1",
        "context_version": "context-v1",
        "description": (
            "加权证据口径：同一特征出现次数参与累加，最多计三次。"
            if scheme == "weighted_evidence"
            else "二值存在口径：特征只要出现即计一次，并叠加来源权重。"
        ),
        "rules": rules,
        "sense_priors": {
            "T_PROTOCOL": {"1": 0.0, "2": 0.0},
            "T_BUS": {"1": 0.0, "2": 0.0},
            "T_CACHE": {"1": 0.0, "2": 0.0},
            "T_MEMORY": {"1": 0.0, "2": 0.0},
            "T_TRANSACTION": {"1": 0.0, "2": 0.0},
            "T_PORT": {"1": 0.0, "2": 0.0},
        },
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _anchors(text: str, anchor: str) -> list[int]:
    positions: list[int] = []
    cursor = 0
    while True:
        index = text.find(anchor, cursor)
        if index < 0:
            return positions
        positions.append(index)
        cursor = index + 1


def generate(output: Path) -> dict[str, Any]:
    output = output.resolve()
    rng = random.Random(SEED)

    docs: list[dict[str, Any]] = []
    for number in range(SHARD_COUNT * DOCS_PER_SHARD):
        template = TEMPLATES[number % len(TEMPLATES)]
        text = f"工单D{number + 1:04d}；" + template["text"]
        docs.append(
            {
                "template": template,
                "doc_id": f"doc-{number + 1:04d}",
                "para_no": 1,
                "text": text,
                "char_len": len(text),
            }
        )
    rng.shuffle(docs)

    shard_paths = []
    shard_rows: list[list[dict[str, Any]]] = [
        [] for _ in range(SHARD_COUNT)
    ]
    for index, doc in enumerate(docs):
        shard_id = f"shard-{index % SHARD_COUNT:03d}"
        shard_rows[index % SHARD_COUNT].append(
            {
                "shard_id": shard_id,
                "doc_id": doc["doc_id"],
                "para_no": doc["para_no"],
                "text": doc["text"],
                "char_len": doc["char_len"],
            }
        )

    shards_dir = output / "data" / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)
    for shard_index, rows in enumerate(shard_rows):
        shard_id = f"shard-{shard_index:03d}"
        rows.sort(key=lambda row: row["doc_id"])
        for row in rows:
            row["shard_id"] = shard_id
        path = shards_dir / f"{shard_id}.jsonl"
        path.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
                for row in rows
            ),
            encoding="utf-8",
            newline="\n",
        )
        shard_paths.append(f"data/shards/{path.name}")

    glossary = {
        "glossary_version": "glossary-v1",
        "entries": sorted(GLOSSARY, key=lambda entry: entry["entry_no"]),
    }
    _write_json(output / "data" / "glossary.json", glossary)

    observations: list[dict[str, Any]] = []
    docs_by_id = {doc["doc_id"]: doc for doc in docs}
    for doc in sorted(docs, key=lambda item: item["doc_id"]):
        for feature_id, anchor in doc["template"]["features"]:
            positions = _anchors(doc["text"], anchor)
            if not positions:
                raise RuntimeError(f"anchor {anchor!r} not found in {doc['doc_id']}")
            for start in positions:
                observations.append(
                    {
                        "doc_id": doc["doc_id"],
                        "para_no": doc["para_no"],
                        "feature_id": feature_id,
                        "start": start,
                        "end": start + len(anchor),
                        "value": anchor,
                    }
                )
    observations.sort(
        key=lambda row: (
            row["doc_id"],
            row["para_no"],
            row["start"],
            row["end"],
            row["feature_id"],
        )
    )
    context = {
        "context_version": "context-v1",
        "catalog": sorted(FEATURE_CATALOG, key=lambda item: item["feature_id"]),
        "observations": observations,
    }
    _write_json(output / "data" / "context_features.json", context)

    rules_dir = output / "data" / "rules"
    _write_json(
        rules_dir / "rules-v1.json",
        _rules("rules-v1", "weighted_evidence", 1.0),
    )
    _write_json(
        rules_dir / "rules-v2.json",
        _rules("rules-v2", "binary_presence", 1.08),
    )

    config = {
        "config_version": "cfg-v1",
        "seed": SEED,
        "normalization": "NFC",
        "glossary_version": "glossary-v1",
        "context_version": "context-v1",
        "rule_version": "rules-v1",
        "scoring": {
            "length_weights": {"S": 0.25, "M": 0.75, "L": 1.25},
            "source_weight_scale": 1.0,
            "coverage_weight": 1.0,
            "context_window_chars": 8,
            "round_digits": 12,
        },
        "adaptive": {
            "exact_max_shards": 6,
            "exact_max_candidates_per_doc": 24,
            "exact_max_avg_features": 12,
            "exact_min_memory_headroom_mb": 64.0,
            "resident_memory_headroom_mb": 128.0,
            "approx_max_candidates_per_doc": 16,
            "approx_max_features_per_candidate": 8,
        },
        "budget": {
            "total_compare_budget": 500000,
            "max_resident_cache_mb": 128.0,
            "candidate_compare_cost": 2,
            "feature_compare_cost": 1,
            "bytes_per_candidate": 384,
            "bytes_per_feature": 96,
            "fixed_doc_bytes": 2048,
            "policy": "strict",
        },
        "disambiguation": {
            "tie_break": "sense_id_asc",
            "comparison_recall_floor": 1.0,
        },
        "paths": {
            "shards": shard_paths,
            "glossary": "data/glossary.json",
            "context_features": "data/context_features.json",
            "rules_dir": "data/rules",
            "snapshot_dir": "snapshots",
            "out_dir": "out",
        },
    }
    _write_json(output / "config.json", config)

    manifest = {
        "seed": SEED,
        "shard_count": SHARD_COUNT,
        "doc_count": len(docs),
        "document_ids": sorted(docs_by_id),
        "observation_count": len(observations),
        "glossary_entry_count": len(GLOSSARY),
        "rule_versions": ["rules-v1", "rules-v2"],
        "engine_note": "No network, clock, or nondeterministic entropy is used at runtime.",
    }
    _write_json(output / "data" / "generation_manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="project root that receives data/ and config.json",
    )
    args = parser.parse_args()
    manifest = generate(args.output)
    print(json.dumps({"ok": True, **manifest}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

