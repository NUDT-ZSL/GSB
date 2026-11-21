from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .canonical import atomic_write_text, canonical_json, canonical_jsonl, object_checksum

DEFAULT_SEED = 20261122

LABELS_V1 = [
    "n",
    "v",
    "a",
    "d",
    "m",
    "q",
    "p",
    "u",
    "w",
    "x",
]
LABELS_V2 = ["n", "v", "a", "m", "q", "p", "u", "w", "x"]

PRIORS = {
    "n": 1.0,
    "v": 0.94,
    "a": 0.72,
    "d": 0.58,
    "m": 0.52,
    "q": 0.48,
    "p": 0.42,
    "u": 0.36,
    "w": 0.20,
    "x": 0.10,
}

BASE_ENTRIES: list[tuple[str, int, list[str]]] = [
    ("数据", 120, ["n"]),
    ("快照", 100, ["n"]),
    ("分词", 90, ["n", "v"]),
    ("标注", 86, ["n", "v"]),
    ("版本", 72, ["n"]),
    ("阶段", 82, ["n"]),
    ("校验", 76, ["n", "v"]),
    ("流水", 60, ["n"]),
    ("文本", 70, ["n"]),
    ("词典", 88, ["n"]),
    ("恢复", 66, ["v", "n"]),
    ("一致性", 40, ["n"]),
    ("偏移", 56, ["n"]),
    ("路径", 64, ["n"]),
    ("标签", 72, ["n"]),
    ("系统", 54, ["n"]),
    ("提交", 68, ["v"]),
    ("替换", 45, ["v"]),
    ("保持", 34, ["v"]),
    ("读取", 52, ["v"]),
    ("计算", 58, ["v"]),
    ("标记", 42, ["v"]),
    ("汇总", 49, ["v", "n"]),
    ("处理", 44, ["v"]),
    ("终止", 35, ["v"]),
    ("生成", 62, ["v"]),
    ("确定", 32, ["a", "v"]),
    ("稳定", 28, ["a"]),
    ("完整", 46, ["a"]),
    ("有效", 26, ["a"]),
    ("一致", 40, ["a"]),
    ("本地", 52, ["a", "n"]),
    ("重新", 35, ["d"]),
    ("已经", 30, ["d"]),
    ("不", 104, ["d"]),
    ("后", 95, ["p"]),
    ("的", 210, ["u"]),
    ("了", 180, ["u"]),
    ("在", 160, ["p"]),
    ("与", 140, ["p"]),
    ("和", 130, ["p"]),
    ("从", 120, ["p"]),
    ("到", 100, ["p"]),
    ("为", 95, ["p"]),
    ("并", 90, ["p"]),
    ("可", 60, ["v"]),
    ("一", 80, ["m"]),
    ("二", 50, ["m"]),
    ("三", 40, ["m"]),
    ("个", 70, ["q"]),
    ("次", 61, ["q"]),
    ("条", 53, ["q"]),
    ("分词", 90, ["n", "v"]),
    ("码点", 55, ["n"]),
    ("候选", 51, ["n"]),
    ("结果", 67, ["n"]),
    ("输入", 63, ["n", "v"]),
    ("原始", 38, ["a"]),
    ("调整", 33, ["v"]),
    ("影响", 39, ["v", "n"]),
    ("行", 48, ["n", "q"]),
    ("最后", 57, ["n"]),
    ("。", 320, ["w"]),
    ("，", 300, ["w"]),
]

TEMPLATES = [
    "本地系统生成快照并提交校验结果。",
    "词典版本变化后重新计算受影响的分词路径。",
    "恢复流程读取最后一次已提交的阶段快照。",
    "标注阶段只读取分词快照而不读取原始输入。",
    "快照恢复保持一致性，重新计算偏移与标签。",
    "本地系统生成稳定快照，校验和保持完整。",
    "分词路径确定后提交标签并汇总结果。",
    "版本替换读取旧快照并标记有效的偏移。",
    "系统从最后一次快照恢复并重新计算路径。",
    "词典生成候选边，标注读取路径分数。",
    "一致性与恢复流程保持稳定。",
    "快照版本确定后提交校验结果。",
    "文本偏移按码点计算并保持有效。",
    "标签集合调整后重新计算受影响的行。",
    "本地系统读取快照并生成完整汇总。",
    "快照恢复读取旧版本并保持一致性。",
    "分词阶段生成候选路径，标注阶段计算标签。",
    "系统并保持稳定，恢复流程确定有效。",
    "校验阶段读取文本并生成完整偏移。",
    "标注结果确定后提交完整标签。",
    "版本变化后系统读取最后一次快照。",
    "恢复流程提交结果并保持一致性。",
    "分词标注流水保持本地确定性。",
    "快照版本与标签版本必须一致。",
    "文本校验读取码点并标记偏移。",
    "词典候选路径确定后生成标注。",
]


def _entry_map(labels: list[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for word, freq, pos in BASE_ENTRIES:
        if word in seen:
            continue
        seen.add(word)
        entries.append({"word": word, "freq": freq, "pos": list(pos)})
    return entries


def _labels_document(version: int, labels: list[str]) -> dict[str, Any]:
    priors = {label: PRIORS[label] for label in labels}
    body: dict[str, Any] = {
        "version": version,
        "labels": labels,
        "priors": priors,
    }
    body["checksum"] = object_checksum(body)
    return body


def _dictionary_document(
    version: int,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(
        (
            {
                "word": item["word"],
                "freq": item["freq"],
                "pos": sorted(set(item["pos"])),
            }
            for item in entries
        ),
        key=lambda item: item["word"],
    )
    body: dict[str, Any] = {"version": version, "entries": ordered}
    body["checksum"] = object_checksum(body)
    return body


def _make_dictionary_v2(v1_entries: list[dict[str, Any]]) -> dict[str, Any]:
    entries = [dict(item) for item in v1_entries]
    for item in entries:
        if item["word"] in {"重新", "已经", "不"}:
            item["pos"] = ["v"]
    additions = [
        {"word": "本地系统", "freq": 34, "pos": ["n"]},
        {"word": "重新计算", "freq": 31, "pos": ["v"]},
        {"word": "快照恢复", "freq": 37, "pos": ["v", "n"]},
        {"word": "码点计算", "freq": 26, "pos": ["n"]},
        {"word": "分词路径", "freq": 29, "pos": ["n"]},
        {"word": "完整汇总", "freq": 22, "pos": ["n"]},
        {"word": "阶段快照", "freq": 24, "pos": ["n"]},
        {"word": "候选路径", "freq": 21, "pos": ["n"]},
    ]
    existing = {item["word"] for item in entries}
    for item in additions:
        if item["word"] not in existing:
            entries.append(item)
    return _dictionary_document(2, entries)


def _write_json(path: Path, value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, text)


def generate_samples(root: str | Path, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    root = Path(root)
    invalid_dir = root / "invalid"
    root.mkdir(parents=True, exist_ok=True)
    invalid_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)
    templates = list(TEMPLATES)
    rng.shuffle(templates)
    selected = templates[:24] + ["甲乙丙丁", "戊己庚辛"]
    lines: list[dict[str, Any]] = []
    for index, text in enumerate(selected, start=1):
        lines.append(
            {
                "doc_id": f"D{index:04d}",
                "line_no": index,
                "text": text,
                "punct_class": "terminal" if text.endswith("。") else "none",
            }
        )

    labels_v1 = _labels_document(1, LABELS_V1)
    labels_v2 = _labels_document(2, LABELS_V2)
    entries_v1 = _entry_map(LABELS_V1)
    dict_v1 = _dictionary_document(1, entries_v1)
    dict_v2 = _make_dictionary_v2(entries_v1)

    _write_json(root / "labels_v1.json", labels_v1)
    _write_json(root / "labels_v2.json", labels_v2)
    _write_json(root / "dict_v1.json", dict_v1)
    _write_json(root / "dict_v2.json", dict_v2)
    atomic_write_text(root / "lines.jsonl", canonical_jsonl(lines))

    default_config = {
        "algorithm_version": "1.0.0",
        "max_codepoints_per_line": 240,
        "max_candidate_edges_per_stage": 20000,
        "candidate_limit_policy": "degrade",
        "degrade_max_candidates_per_start": 3,
        "max_tag_resident_bytes": 8000000,
        "tag_memory_policy": "degrade",
        "fallback_pos": "x",
    }
    low_budget_config = {
        **default_config,
        "max_candidate_edges_per_stage": 8,
        "degrade_max_candidates_per_start": 2,
        "max_tag_resident_bytes": 900,
        "tag_memory_policy": "degrade",
    }
    strict_config = {
        **default_config,
        "max_candidate_edges_per_stage": 3,
        "candidate_limit_policy": "error",
        "max_tag_resident_bytes": 120,
        "tag_memory_policy": "error",
    }
    _write_json(root / "config_default.json", default_config)
    _write_json(root / "config_low_budget.json", low_budget_config)
    _write_json(root / "config_strict.json", strict_config)

    invalid_dup = [
        {"doc_id": "D9001", "line_no": 1, "text": "重复文档。", "punct_class": "terminal"},
        {"doc_id": "D9001", "line_no": 2, "text": "再次重复。", "punct_class": "terminal"},
    ]
    invalid_line_no = [
        {"doc_id": "D9101", "line_no": 1, "text": "第一条。", "punct_class": "terminal"},
        {"doc_id": "D9102", "line_no": 3, "text": "跳过第二条。", "punct_class": "terminal"},
    ]
    invalid_empty = [
        {"doc_id": "D9201", "line_no": 1, "text": "   ", "punct_class": "none"},
    ]
    invalid_punct = [
        {"doc_id": "D9301", "line_no": 1, "text": "非法标点类别。", "punct_class": "other"},
    ]
    atomic_write_text(invalid_dir / "dup_doc.jsonl", canonical_jsonl(invalid_dup))
    atomic_write_text(invalid_dir / "line_no.jsonl", canonical_jsonl(invalid_line_no))
    atomic_write_text(invalid_dir / "empty_text.jsonl", canonical_jsonl(invalid_empty))
    atomic_write_text(invalid_dir / "punct_class.jsonl", canonical_jsonl(invalid_punct))

    bad_dict = {
        "version": 1,
        "entries": [{"word": "", "freq": 0, "pos": ["n"]}],
    }
    bad_label_dict = {
        "version": 1,
        "entries": [{"word": "越界", "freq": 10, "pos": ["z"]}],
    }
    _write_json(invalid_dir / "dict_format.json", bad_dict)
    _write_json(invalid_dir / "dict_label.json", bad_label_dict)
    _write_json(
        invalid_dir / "bad_offsets.json",
        {
            "doc_id": "D9401",
            "line_no": 1,
            "text": "测试",
            "tokens": [
                {
                    "word": "测",
                    "start": 0,
                    "end": 2,
                    "pos_candidates": ["n"],
                }
            ],
        },
    )

    manifest = {
        "seed": seed,
        "line_count": len(lines),
        "template_count": len(TEMPLATES),
        "labels_v1_checksum": labels_v1["checksum"],
        "labels_v2_checksum": labels_v2["checksum"],
        "dict_v1_checksum": dict_v1["checksum"],
        "dict_v2_checksum": dict_v2["checksum"],
        "lines_checksum": object_checksum(lines),
    }
    _write_json(root / "manifest.json", manifest)
    return manifest
