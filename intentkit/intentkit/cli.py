"""命令行示例：读取本地确定性样本并输出完整 JSON 报告。

报告包含意图结果、码点偏移、路径取舍记录、跨路径偏差谱、
增量重算/复用清单、快照替换、业务码与重放校验结论。
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .budget import BudgetLimits
from .cache import (
    ContentStore,
    fingerprint_results,
    plan_incremental,
    run_incremental,
    tickets_hitting_rules,
)
from .engine import run_engine
from .errors import BizError
from .generator import (
    build_config_v1,
    build_config_v2,
    build_config_v3,
    correct_gold_label,
    generate_tickets,
)
from .lineage import replay_all
from .matcher import match_exact
from .snapshot import SnapshotStore
from .validation import validate_config, validate_tickets


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def build_demo_report(workdir: Path, ticket_count: int = 96) -> Dict[str, Any]:
    """编排全部验收场景并组装可复现 JSON 报告。"""
    cache_dir = workdir / "cache"
    snapshot_dir = workdir / "snapshots"
    # 演示需要从 v1 单调演进到 v3；清掉历史运行产物以避免触发回退保护。
    shutil.rmtree(cache_dir, ignore_errors=True)
    shutil.rmtree(snapshot_dir, ignore_errors=True)
    store = ContentStore(cache_dir)
    snapshot_store = SnapshotStore(snapshot_dir)

    report: Dict[str, Any] = {"scenarios": {}}

    config_v1 = build_config_v1()
    tickets_v1 = generate_tickets(count=ticket_count)

    report["input_validation"] = _validation_demo(tickets_v1, config_v1)

    save_v1 = snapshot_store.save(config_v1)
    limits = BudgetLimits(max_total_comparisons=2_000_000, max_resident_bytes=1_000_000)
    engine_v1 = run_engine(tickets_v1, config_v1, limits)
    report["scenarios"]["v1_baseline"] = {
        "snapshot": save_v1,
        "decision": engine_v1.decision.to_dict(),
        "admission": engine_v1.admission.to_dict(),
        "consistency": engine_v1.consistency_report,
        "sample_results": [item.to_dict() for item in engine_v1.selected_results[:8]],
        "result_total": len(engine_v1.selected_results),
    }

    for result in engine_v1.selected_results:
        store.put(result.cache_key, result.to_dict())

    report["scenarios"]["replay_v1"] = replay_all(
        tickets_v1,
        config_v1,
        [item.to_dict() for item in engine_v1.selected_results],
        store=store,
    ).to_dict()

    config_v2 = build_config_v2()
    save_v2 = snapshot_store.replace_whole(config_v2)
    threshold_only_rules = [
        label
        for label in ("logistics_query",)
    ]
    threshold_invalid_ids = tickets_hitting_rules(
        tickets_v1, config_v2, threshold_only_rules
    )
    plan_v2 = plan_incremental(
        tickets_v1,
        tickets_v1,
        config_v1,
        config_v2,
        engine_v1.decision.path,
        store,
        extra_invalid_ticket_ids=threshold_invalid_ids,
    )
    results_v2_inc, recomputed_v2 = run_incremental(
        tickets_v1, config_v2, engine_v1.decision.path, plan_v2, store
    )
    engine_v2_full = run_engine(tickets_v1, config_v2, limits)
    full_v2 = (
        engine_v2_full.exact_results
        if engine_v1.decision.path == "exact"
        else engine_v2_full.approximate_results
    )
    report["scenarios"]["v2_config_evolution"] = {
        "snapshot": save_v2,
        "incremental_plan": plan_v2.to_dict(),
        "recomputed_total": len(recomputed_v2),
        "full_fingerprint": fingerprint_results(full_v2),
        "incremental_fingerprint": fingerprint_results(results_v2_inc),
        "byte_identical": fingerprint_results(full_v2)
        == fingerprint_results(results_v2_inc),
    }

    corrected_tickets = correct_gold_label(
        tickets_v1, tickets_v1[3].ticket_id, "payment_issue"
    )
    plan_gold = plan_incremental(
        tickets_v1, corrected_tickets, config_v1, config_v1, "exact", store
    )
    gold_results, gold_recomputed = run_incremental(
        corrected_tickets, config_v1, "exact", plan_gold, store
    )
    gold_full = [match_exact(ticket, config_v1) for ticket in corrected_tickets]
    report["scenarios"]["gold_correction"] = {
        "corrected_ticket_id": tickets_v1[3].ticket_id,
        "incremental_plan": plan_gold.to_dict(),
        "recomputed_total": len(gold_recomputed),
        "full_fingerprint": fingerprint_results(gold_full),
        "incremental_fingerprint": fingerprint_results(gold_results),
        "byte_identical": fingerprint_results(gold_full)
        == fingerprint_results(gold_results),
    }

    config_v3 = build_config_v3()
    save_v3 = snapshot_store.replace_whole(config_v3)
    engine_v3 = run_engine(tickets_v1, config_v3, limits)
    fallback_records = [
        item.to_dict()
        for item in engine_v3.selected_results
        if item.fallback_category == "retired_label"
    ]
    report["scenarios"]["v3_label_retirement"] = {
        "snapshot": save_v3,
        "controlled_labels": list(config_v3.controlled_labels),
        "retired_fallback_total": len(fallback_records),
        "sample_retired_fallbacks": fallback_records[:5],
    }

    report["error_chain"] = _error_chain_demo(
        snapshot_store, config_v1, config_v3, tickets_v1
    )
    report["budget_chain"] = _budget_chain_demo(tickets_v1, config_v1)
    report["divergence_chain"] = _divergence_demo(tickets_v1, config_v1)
    report["determinism"] = _determinism_demo(tickets_v1, config_v1, limits)
    return report


def _validation_demo(tickets: Sequence, config) -> Dict[str, Any]:
    from .models import Ticket

    bad_tickets = list(tickets[:4]) + [tickets[0]]
    bad_tickets.append(Ticket(ticket_id="TK_EMPTY", query_text="", locale="zh-CN"))
    issues = validate_tickets(bad_tickets)
    config_issues = validate_config(config)
    return {
        "ticket_issues": [issue.to_dict() for issue in issues],
        "config_issues": [issue.to_dict() for issue in config_issues],
        "ticket_issue_codes": sorted({issue.code for issue in issues}),
    }


def _error_chain_demo(snapshot_store, config_v1, config_v3, tickets) -> Dict[str, Any]:
    from .models import IntentConfig
    from .validation import validate_rule_dict

    findings: List[Dict[str, Any]] = []

    try:
        rolled_back = IntentConfig.build(
            version=1,
            controlled_labels=config_v3.controlled_labels,
            rules=config_v3.rules,
            default_fallback_label=config_v3.default_fallback_label,
        )
        snapshot_store.save(rolled_back)
    except BizError as error:
        findings.append(error.to_dict())

    try:
        snapshot_store.load(version=999)
    except BizError as error:
        findings.append(error.to_dict())

    bad_rule = {
        "label": "not_a_controlled_label",
        "keywords": ["", "退款"],
        "threshold": 1.7,
        "fallback_label": "ghost_label",
        "batch_size": 16,
    }
    rule_issues = validate_rule_dict(bad_rule, config_v1.controlled_labels)
    findings.extend(issue.to_dict() for issue in rule_issues)
    return {
        "findings": findings,
        "codes": sorted({item["code"] for item in findings}),
    }


def _budget_chain_demo(tickets, config) -> Dict[str, Any]:
    tight = BudgetLimits(max_total_comparisons=20_000, max_resident_bytes=1_000_000)
    tight_output = run_engine(tickets, config, tight)
    memory_limit = BudgetLimits(max_total_comparisons=10**12, max_resident_bytes=1)
    memory_case: Dict[str, Any] = {"limits": memory_limit.__dict__}
    try:
        run_engine(tickets, config, memory_limit)
    except BizError as error:
        memory_case["error"] = error.to_dict()
    return {
        "tight_budget": {
            "admission": tight_output.admission.to_dict(),
            "admitted_ticket_total": tight_output.admission.admitted_ticket_total,
            "rejected_ticket_total": tight_output.admission.rejected_ticket_total,
            "downgrade_category": tight_output.admission.downgrade_category,
        },
        "memory_limit": memory_case,
    }


def _divergence_demo(tickets, config) -> Dict[str, Any]:
    from .matcher import match_approximate, match_exact

    force_skip = ("logistics_query", "快递单号")
    exact_results = [match_exact(ticket, config) for ticket in tickets]
    approx_results = [
        match_approximate(ticket, config, force_skip=force_skip) for ticket in tickets
    ]
    case: Dict[str, Any] = {"forced_skip": {"label": force_skip[0], "keyword": force_skip[1]}}
    try:
        run_engine(tickets, config, BudgetLimits(10**12, 10**12), force_skip=force_skip)
    except BizError as error:
        case["error"] = error.to_dict()
    from .consistency import adjudicate

    case["non_raising_report"] = adjudicate(
        exact_results, approx_results, raise_on_divergence=False
    )
    return case


def _determinism_demo(tickets, config, limits) -> Dict[str, Any]:
    run_a = run_engine(tickets, config, limits)
    run_b = run_engine(tickets, config, limits)
    payload_a = json.dumps(
        [item.to_dict() for item in run_a.selected_results],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload_b = json.dumps(
        [item.to_dict() for item in run_b.selected_results],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "byte_identical": payload_a == payload_b,
        "result_fingerprint": fingerprint_results(run_a.selected_results),
        "decision_bytes": json.dumps(
            run_a.decision.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="intentkit 本地确定性意图识别示例")
    parser.add_argument(
        "--workdir",
        default=str(Path(__file__).resolve().parent.parent / "data"),
        help="缓存与快照落地目录（Windows 本地可写路径亦可）",
    )
    parser.add_argument("--tickets", type=int, default=96, help="确定性工单数量")
    parser.add_argument(
        "--report",
        default=None,
        help="JSON 报告输出路径，默认 <workdir>/report.json",
    )
    args = parser.parse_args(argv)

    workdir = Path(args.workdir)
    report = build_demo_report(workdir, ticket_count=args.tickets)
    report_path = Path(args.report) if args.report else workdir / "report.json"
    _write_json(report_path, report)
    print(json.dumps({"report_path": str(report_path), "status": "ok"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
