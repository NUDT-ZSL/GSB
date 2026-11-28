from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .common import (
    canonical_core,
    read_json,
    sha256_file,
    sha256_value,
    stable_json,
    write_json,
)
from .comparison import (
    build_difference_report,
    cross_version_report,
    first_reference_mismatch,
)
from .engine import run_main
from .generator import SCHEMA_NAMES, write_samples
from .normalizer import normalize_release
from .reference_runner import run_reference


def _relative(path: Path, workdir: Path) -> str:
    try:
        return path.resolve().relative_to(workdir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def _ledger_csv(run: dict[str, Any], path: Path) -> None:
    rows = []
    for entry in run["ledger"]:
        row = {
            "step_number": entry["step_number"],
            "step_id": entry["step_id"],
            "traffic_percent": entry["declared_traffic_percent"],
            "verdict": entry["verdict"],
            "elapsed_window_seconds": entry["elapsed_window_seconds"],
            "actual_rollout_seconds": entry["actual_rollout_seconds"],
            "paused_seconds": entry["paused_seconds"],
            "pause_count": entry["pause_count"],
            "rollback_count": entry["rollback_count"],
        }
        for metric, counts in entry["metrics"].items():
            row[f"{metric}.breach_count"] = counts["breach_count"]
            row[f"{metric}.spike_count"] = counts["spike_count"]
            row[f"{metric}.sustained_breach_count"] = counts[
                "sustained_breach_count"
            ]
        rows.append(row)
    fields = [
        "step_number",
        "step_id",
        "traffic_percent",
        "verdict",
        "elapsed_window_seconds",
        "actual_rollout_seconds",
        "paused_seconds",
        "pause_count",
        "rollback_count",
    ]
    for metric in ("error_rate", "latency_p95_ms", "session_drop_rate"):
        fields.extend(
            [
                f"{metric}.breach_count",
                f"{metric}.spike_count",
                f"{metric}.sustained_breach_count",
            ]
        )
    _write_csv(path, rows, fields)


def _decision_csv(run: dict[str, Any], path: Path) -> None:
    rows = []
    for decision in run["step_decisions"]:
        trigger = decision.get("trigger") or {}
        rows.append(
            {
                "step_number": decision["step_number"],
                "step_id": decision["step_id"],
                "traffic_percent": decision["declared_traffic_percent"],
                "verdict": decision["verdict"],
                "trigger_metric": trigger.get("metric", ""),
                "trigger_observed": trigger.get("observed", ""),
                "trigger_threshold": trigger.get("threshold", ""),
                "trigger_consecutive": trigger.get("consecutive_breaches", ""),
                "pause_episode_count": len(decision["pause_evidence"]),
                "rollback_target_step": (
                    decision["rollback_target"]["step_number"]
                    if decision.get("rollback_target")
                    else ""
                ),
            }
        )
    _write_csv(
        path,
        rows,
        [
            "step_number",
            "step_id",
            "traffic_percent",
            "verdict",
            "trigger_metric",
            "trigger_observed",
            "trigger_threshold",
            "trigger_consecutive",
            "pause_episode_count",
            "rollback_target_step",
        ],
    )


def _scenario_checks(
    normalized_by_schema: dict[str, dict[str, Any]],
    runs_by_schema: dict[str, dict[str, Any]],
) -> dict[str, bool]:
    baseline = normalized_by_schema["v1"]
    run = runs_by_schema["v1"]
    decisions = {item["step_number"]: item for item in run["step_decisions"]}
    ledger = {item["step_number"]: item for item in run["ledger"]}
    rollback = run["rollback"]

    spike_step = decisions.get(1, {})
    spike_ledger = ledger.get(1, {}).get("metrics", {}).get("error_rate", {})
    missing_step = decisions.get(2, {})
    rollback_step = decisions.get(4, {})
    rollback_trigger = rollback_step.get("trigger") or {}
    target_step = (
        rollback.get("target", {}).get("step_number")
        if rollback
        else None
    )
    prior_promoted = [
        item["step_number"]
        for item in run["step_decisions"]
        if item["step_number"] < 4 and item["verdict"] == "promote"
    ]
    decision_ledger_aligned = all(
        decision["ledger_ref"] == ledger_entry["entry_id"]
        and decision["verdict"] == ledger_entry["verdict"]
        for decision, ledger_entry in zip(
            run["step_decisions"], run["ledger"]
        )
    )
    return {
        "isolated_spike_does_not_rollback": (
            spike_step.get("verdict") == "promote"
            and spike_ledger.get("spike_count", 0) >= 1
            and spike_ledger.get("sustained_breach_count", 0) == 0
        ),
        "probe_missing_pauses_and_recovers": (
            bool(missing_step.get("pause_evidence"))
            and bool(missing_step.get("resume_evidence"))
            and missing_step.get("verdict") == "promote"
            and ledger.get(2, {}).get("pause_count", 0) == 1
        ),
        "sustained_breach_rolls_back": (
            rollback_step.get("verdict") == "rollback"
            and rollback_trigger.get("kind") == "sustained_metric_breach"
            and rollback_trigger.get("consecutive_breaches")
            == rollback_trigger.get("required_consecutive_breaches")
        ),
        "rollback_target_is_recent_stable_step": (
            target_step is not None
            and target_step == max(prior_promoted, default=0)
        ),
        "ledger_and_decisions_aligned": decision_ledger_aligned,
        "all_declared_steps_accounted": (
            len(decisions) == len(baseline["steps"])
            and len(ledger) == len(baseline["steps"])
        ),
    }


def _render_report(
    seed: int,
    normalized_by_schema: dict[str, dict[str, Any]],
    runs_by_schema: dict[str, dict[str, Any]],
    differences: dict[str, Any],
    verification: dict[str, Any],
) -> str:
    run = runs_by_schema["v1"]
    project = normalized_by_schema["v1"]
    lines = [
        "# 远程教学渐进发布离线复核报告",
        "",
        "## 结论",
        "",
        f"- 固定随机种子：`{seed}`",
        f"- 输入 schema：`v1`、`v2`、`v3`",
        f"- 全部验收检查：**{'通过' if verification['all_passed'] else '未通过'}**",
        "- 三版本归一核心、逐步裁决、回退目标与放量账目："
        f"{'一致' if verification['cross_version']['runs_equal'] else '不一致'}",
        "- 独立参考推进器："
        f"{'全部步骤一致' if verification['reference_all_match'] else '存在不一致'}",
        "",
        "## 逐步裁决",
        "",
        "| 步骤 | 流量 | 最终裁决 | 最终触发/原因 | 暂停次数 | 回退目标 |",
        "|---:|---:|---|---|---:|---|",
    ]
    for decision in run["step_decisions"]:
        trigger = decision.get("trigger") or {}
        if decision["verdict"] == "not_evaluated":
            trigger_text = "未执行（前序步骤已回滚或暂停）"
        elif trigger.get("kind") == "sustained_metric_breach":
            trigger_text = (
                f"{trigger['metric']} 观测 {trigger['observed']} > "
                f"{trigger['threshold']}，连续 {trigger['consecutive_breaches']} 次"
            )
        elif not decision["pause_evidence"]:
            trigger_text = "观察窗口完成，无持续超阈"
        else:
            trigger_text = "探针缺失暂停后恢复，无持续超阈"
        target = (
            str(decision["rollback_target"]["step_number"])
            if decision.get("rollback_target")
            else "-"
        )
        lines.append(
            f"| {decision['step_number']} | {decision['declared_traffic_percent']}% "
            f"| {decision['verdict']} | {trigger_text} "
            f"| {len(decision['pause_evidence'])} | {target} |"
        )

    lines.extend(["", "## 回退动作", ""])
    rollback = run["rollback"]
    if rollback:
        lines.extend(
            [
                f"- 失败步骤：`{rollback['failed_step']['step_number']}` "
                f"({rollback['failed_step']['step_id']})，流量 "
                f"`{rollback['failed_step']['traffic_percent']}%`。",
                f"- 回退目标：步骤 `{rollback['target']['step_number']}` "
                f"({rollback['target']['step_id']})，流量 "
                f"`{rollback['target']['traffic_percent']}%`。",
                f"- 回收流量：`{rollback['traffic_recovery_percent']}%` "
                f"(`{rollback['traffic_recovery_ppm']} ppm`)。",
                "- 重新观察窗口："
                f"`{rollback['reobserve_window']['logical_start_seconds']}` 至 "
                f"`{rollback['reobserve_window']['logical_end_seconds']}` 秒。",
                f"- 受影响会话：`{rollback['affected_session_count']}` 个。",
            ]
        )
        for item in rollback["affected_session_dispositions"]:
            lines.append(
                f"- `{item['cohort']}`：{item['count']} 个，"
                f"会话范围 `{item['session_range']}`，处置 "
                f"`{item['disposition']}`。"
            )
        lines.append("")
        lines.append("| 序号 | 动作 | 说明 |")
        lines.append("|---:|---|---|")
        for action in rollback["actions"]:
            lines.append(
                f"| {action['sequence']} | {action['action']} | "
                f"{action['detail']} |"
            )
    else:
        lines.append("- 未触发回滚。")

    lines.extend(
        [
            "",
            "## 放量账目",
            "",
            "| 步骤 | 流量 | 裁决 | 实际放量秒 | 暂停秒 | 暂停次数 | 回滚次数 | "
            "错误率 超阈/尖峰 | 延迟 超阈/尖峰 | 掉线率 超阈/尖峰 |",
            "|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for entry in run["ledger"]:
        metrics = entry["metrics"]
        lines.append(
            f"| {entry['step_number']} | {entry['declared_traffic_percent']}% "
            f"| {entry['verdict']} | {entry['actual_rollout_seconds']} "
            f"| {entry['paused_seconds']} | {entry['pause_count']} "
            f"| {entry['rollback_count']} "
            f"| {metrics['error_rate']['breach_count']}/"
            f"{metrics['error_rate']['spike_count']} "
            f"| {metrics['latency_p95_ms']['breach_count']}/"
            f"{metrics['latency_p95_ms']['spike_count']} "
            f"| {metrics['session_drop_rate']['breach_count']}/"
            f"{metrics['session_drop_rate']['spike_count']} |"
        )

    lines.extend(["", "## 关键验收点", ""])
    for name, passed in verification["scenario_checks"].items():
        lines.append(f"- `{name}`：{'通过' if passed else '失败'}")

    lines.extend(
        [
            "",
            "## 探针缺失与尖峰",
            "",
            f"- 第 1 步错误率出现 {run['ledger'][0]['metrics']['error_rate']['spike_count']} "
            "个非持续尖峰，未触发回滚。",
            "- 第 2 步延迟探针缺失超过 34% 时暂停；缺失滑动值保持为空，"
            "连续计数归零；恢复后重新累计。",
            "- 第 4 步错误率连续 3 个滑动窗口超阈，达到声明次数后回滚。",
            "",
            "## 跨版本差异",
            "",
            f"- 差异字段数：`{differences['field_count']}`。",
            "- 差异类别计数：`"
            + stable_json(differences["kind_counts"])
            + "`。",
            "- 所有字段归一值一致："
            f"`{differences['all_normalized_values_equal']}`。",
            "",
            "| 归一字段 | 差异类别 | v1 来源/原值 | v2 来源/原值 | v3 来源/原值 | 归一值 |",
            "|---|---|---|---|---|---|",
        ]
    )
    for row in differences["fields"]:
        source_cells = []
        for schema in ("v1", "v2", "v3"):
            source = row["sources"][schema]
            raw = stable_json(source["raw_value"])
            if len(raw) > 64:
                raw = raw[:61] + "..."
            source_cells.append(
                f"`{source['path']}` / `{raw}` `{source['raw_unit']}`"
            )
        normalized = stable_json(
            row["normalized_values_by_schema"].get("v1")
        )
        lines.append(
            f"| `{row['canonical_field']}` | "
            f"`{','.join(row['difference_kinds'])}` | "
            + " | ".join(source_cells)
            + f" | `{normalized}` |"
        )

    lines.extend(
        [
            "",
            "## 结果文件",
            "",
            "- `cross_version_differences.json`：完整跨版本差异清单。",
            "- `verification.json`：验收检查、哈希与首个参考不一致项。",
            "- `v1/`、`v2/`、`v3/`：归一工程、主流程结果、参考结果、"
            "逐步裁决、轨迹、回退动作、账目 JSON 与 CSV。",
            "- `summary.json`：运行入口与本报告摘要。",
            "",
        ]
    )
    return "\n".join(lines)


def run_pipeline(
    *, workdir: Path, seed: int, samples_dir: Path, output_dir: Path
) -> dict[str, Any]:
    workdir = workdir.resolve()
    samples_dir = samples_dir.resolve()
    output_dir = output_dir.resolve()
    sample_paths = write_samples(samples_dir, seed)

    normalized_by_schema: dict[str, dict[str, Any]] = {}
    runs_by_schema: dict[str, dict[str, Any]] = {}
    references_by_schema: dict[str, dict[str, Any]] = {}
    files_by_schema: dict[str, dict[str, str]] = {}

    for schema in SCHEMA_NAMES:
        schema_dir = output_dir / schema
        normalized = normalize_release(read_json(sample_paths[schema]))
        run = run_main(normalized)
        reference = run_reference(normalized)
        normalized_by_schema[schema] = normalized
        runs_by_schema[schema] = run
        references_by_schema[schema] = reference

        normalized_path = schema_dir / "normalized.json"
        run_path = schema_dir / "run.json"
        reference_path = schema_dir / "reference.json"
        decisions_path = schema_dir / "step_decisions.json"
        trajectories_path = schema_dir / "metric_trajectories.json"
        rollback_path = schema_dir / "rollback_actions.json"
        ledger_path = schema_dir / "ledger.json"
        ledger_csv_path = schema_dir / "ledger.csv"
        decisions_csv_path = schema_dir / "step_decisions.csv"

        write_json(normalized_path, normalized)
        write_json(run_path, run)
        write_json(reference_path, reference)
        write_json(decisions_path, run["step_decisions"])
        write_json(trajectories_path, run["metric_trajectories"])
        write_json(
            rollback_path,
            run["rollback"]
            if run["rollback"] is not None
            else {"required": False, "actions": []},
        )
        write_json(ledger_path, run["ledger"])
        _ledger_csv(run, ledger_csv_path)
        _decision_csv(run, decisions_path.with_suffix(".csv"))

        files_by_schema[schema] = {
            "input": _relative(sample_paths[schema], workdir),
            "normalized": _relative(normalized_path, workdir),
            "run": _relative(run_path, workdir),
            "reference": _relative(reference_path, workdir),
            "step_decisions_json": _relative(decisions_path, workdir),
            "step_decisions_csv": _relative(decisions_path.with_suffix(".csv"), workdir),
            "metric_trajectories": _relative(trajectories_path, workdir),
            "rollback_actions": _relative(rollback_path, workdir),
            "ledger_json": _relative(ledger_path, workdir),
            "ledger_csv": _relative(ledger_csv_path, workdir),
        }

    differences = build_difference_report(normalized_by_schema)
    reference_mismatches = {
        schema: first_reference_mismatch(
            runs_by_schema[schema], references_by_schema[schema]
        )
        for schema in SCHEMA_NAMES
    }
    cross = cross_version_report(
        normalized_by_schema, runs_by_schema, references_by_schema
    )
    scenario_checks = _scenario_checks(normalized_by_schema, runs_by_schema)
    reference_all_match = all(
        mismatch is None for mismatch in reference_mismatches.values()
    )
    checks = {
        "all_three_schemas_loaded": set(normalized_by_schema) == set(SCHEMA_NAMES),
        "normalized_core_equal": cross["normalized_core_equal"],
        "step_decisions_equal": cross["decisions_equal"],
        "rollback_targets_equal": cross["rollback_targets_equal"],
        "ledgers_equal": cross["ledgers_equal"],
        "cross_version_runs_equal": cross["runs_equal"],
        "reference_runs_equal": cross["reference_runs_equal"],
        "reference_all_match": reference_all_match,
        "difference_values_equal": differences["all_normalized_values_equal"],
    }
    all_passed = all(checks.values()) and all(scenario_checks.values())
    verification = {
        "all_passed": all_passed,
        "checks": checks,
        "scenario_checks": scenario_checks,
        "reference_all_match": reference_all_match,
        "first_reference_mismatch_by_schema": reference_mismatches,
        "cross_version": cross,
        "normalized_core_sha256": {
            schema: sha256_value(canonical_core(normalized_by_schema[schema]))
            for schema in SCHEMA_NAMES
        },
    }

    differences_path = output_dir / "cross_version_differences.json"
    verification_path = output_dir / "verification.json"
    write_json(differences_path, differences)
    write_json(verification_path, verification)

    report = _render_report(
        seed, normalized_by_schema, runs_by_schema, differences, verification
    )
    report_path = output_dir / "report.md"
    report_path.write_text(report + "\n", encoding="utf-8")

    summary = {
        "seed": seed,
        "workdir": ".",
        "samples_dir": _relative(samples_dir, workdir),
        "output_dir": _relative(output_dir, workdir),
        "all_passed": all_passed,
        "checks": checks,
        "scenario_checks": scenario_checks,
        "summary": runs_by_schema["v1"]["summary"],
        "rollback_target": runs_by_schema["v1"]["rollback_target"],
        "cross_version": {
            "normalized_core_equal": cross["normalized_core_equal"],
            "decisions_equal": cross["decisions_equal"],
            "rollback_targets_equal": cross["rollback_targets_equal"],
            "ledgers_equal": cross["ledgers_equal"],
            "difference_field_count": differences["field_count"],
        },
        "files": files_by_schema,
        "review": {
            "step_decisions": [
                {
                    "step_number": item["step_number"],
                    "step_id": item["step_id"],
                    "traffic_percent": item["declared_traffic_percent"],
                    "verdict": item["verdict"],
                    "trigger": item["trigger"],
                    "pause_count": len(item["pause_evidence"]),
                    "rollback_target": item["rollback_target"],
                }
                for item in runs_by_schema["v1"]["step_decisions"]
            ],
            "ledger": runs_by_schema["v1"]["ledger"],
            "rollback_actions": (
                runs_by_schema["v1"]["rollback"]["actions"]
                if runs_by_schema["v1"]["rollback"]
                else []
            ),
            "difference_field_count": differences["field_count"],
            "difference_kind_counts": differences["kind_counts"],
        },
        "reports": {
            "markdown": _relative(report_path, workdir),
            "verification": _relative(verification_path, workdir),
            "cross_version_differences": _relative(differences_path, workdir),
        },
        "hashes": {
            "inputs": {
                schema: sha256_file(sample_paths[schema])
                for schema in SCHEMA_NAMES
            },
            "report": sha256_file(report_path),
        },
    }
    summary_path = output_dir / "summary.json"
    write_json(summary_path, summary)
    return summary

