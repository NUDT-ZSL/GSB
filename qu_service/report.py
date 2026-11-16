"""实验报告：原子写盘，含意图分布、槽位统计、改写条数、注入命中矩阵、
失败清单与各阶段耗时。除 stage_timings 外全部字段可复现（报告指纹自证）。"""
from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

from .hashing import fingerprint
from .snapshot import STAGES

# 可复现性比较时剔除的易变字段
VOLATILE_KEYS = ("stage_timings", "report_fingerprint")


def build_report(run_name: str, config_hash: str, injection_hash: str,
                 queries: list[dict], query_order: list[str],
                 results: dict, failures: list, rule_missing: list,
                 timings: dict, vocab: dict) -> dict:
    by_id = {q["query_id"]: q for q in queries}
    stage_rank = {stage: idx for idx, stage in enumerate(STAGES)}
    order_rank = {qid: idx for idx, qid in enumerate(query_order)}

    query_results = []
    intent_counter: Counter = Counter()
    slot_type_counter: Counter = Counter()
    overlap_total = 0
    rewrite_kind_counter: Counter = Counter()
    rewrite_total = 0
    injection_hits: dict[str, list[str]] = {}
    codes_counter: Counter = Counter()
    labeled = 0
    labeled_correct = 0

    for qid in query_order:
        query = by_id[qid]
        norm = results["normalize"].get(qid)
        intent = results["intent"].get(qid)
        slots = results["slots"].get(qid)
        rewrite = results["rewrite"].get(qid)

        codes: list[str] = []
        for stage_result in (intent, slots, rewrite):
            if stage_result:
                codes.extend(stage_result.get("codes", []))
        injections = list(query.get("injections", []))
        if rewrite:
            injections.extend(rewrite.get("conflict_hits", []))

        primary = intent["primary_intent"] if intent else None
        if primary is not None:
            intent_counter[primary] += 1
        if slots:
            for slot in slots["slots"]:
                slot_type_counter[slot["type"]] += 1
            overlap_total += slots["overlap_count"]
        suggestions = rewrite["suggestions"] if rewrite else []
        for sug in suggestions:
            rewrite_kind_counter[sug["kind"]] += 1
        rewrite_total += len(suggestions)
        for code in codes:
            codes_counter[code] += 1
        for hit in injections:
            injection_hits.setdefault(hit, []).append(qid)

        label = query.get("label_intent")
        if label and primary is not None:
            labeled += 1
            if label == primary:
                labeled_correct += 1

        query_results.append({
            "query_id": qid,
            "normalized_text": norm["text"] if norm else None,
            "primary_intent": primary,
            "intent_scores": intent["scores"] if intent else None,
            "slots": slots["slots"] if slots else None,
            "suppressed": slots["suppressed"] if slots else None,
            "suggestions": suggestions,
            "codes": codes,
            "injections": injections,
            "stage_fingerprints": {
                stage: (results[stage][qid]["fingerprint"] if results[stage].get(qid) else None)
                for stage in STAGES
            },
        })

    sorted_failures = sorted(
        failures,
        key=lambda f: (order_rank.get(f["query_id"], 1 << 30),
                       stage_rank.get(f["stage"], 1 << 30),
                       f["code"], f["sort_key"]))
    for failure in sorted_failures:
        codes_counter[failure["code"]] += 1
    for item in rule_missing:
        codes_counter[item["code"]] += 1

    report = {
        "run_name": run_name,
        "config_hash": config_hash,
        "injection_hash": injection_hash,
        "total_queries": len(query_order),
        "processed": len(query_order) - len({f["query_id"] for f in sorted_failures}),
        "failed": len({f["query_id"] for f in sorted_failures}),
        "intent_distribution": dict(sorted(intent_counter.items())),
        "slot_stats": {
            "per_type": dict(sorted(slot_type_counter.items())),
            "overlap_total": overlap_total,
        },
        "rewrite_stats": {
            "total": rewrite_total,
            "per_kind": dict(sorted(rewrite_kind_counter.items())),
        },
        "label_accuracy": (round(labeled_correct / labeled, 6) if labeled else None),
        "injection_hit_matrix": {k: sorted(v) for k, v in sorted(injection_hits.items())},
        "rule_missing": sorted(rule_missing, key=lambda m: m["rule_id"]),
        "failures": sorted_failures,
        "codes_summary": dict(sorted(codes_counter.items())),
        "stage_timings": {stage: timings.get(stage, 0.0) for stage in STAGES},
        "query_results": query_results,
    }
    report["report_fingerprint"] = fingerprint(
        {k: v for k, v in report.items() if k not in VOLATILE_KEYS})
    return report


def strip_volatile(report: dict) -> dict:
    """剔除易变字段后的可复现视图，用于跨运行逐字段比较。"""
    return {k: v for k, v in report.items() if k not in VOLATILE_KEYS}


def write_report(path: Path, report: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, path)  # 原子替换
