"""本地可交互诊断工作台：把报告渲染为确定性文本视图。"""

from __future__ import annotations

import json

from .spectrum import SPECTRUM_STAGES


def _fmt(x):
    if x is None:
        return "-"
    if isinstance(x, float):
        if x != x:
            return "nan"
        if x == float("inf"):
            return "inf"
        return f"{x:.3e}"
    return str(x)


def render_report(report: dict) -> str:
    lines = []
    add = lines.append
    add("=" * 72)
    add("潜空间分块去噪/解码 数值诊断工作台")
    add(f"workbench={report['workbench_version']}  tasks={report['task_count']}  "
        f"ok={report['ok_count']}  errors={report['error_count']}")
    add(f"report_hash={report['report_hash'][:16]}...")
    add("=" * 72)

    add("[快照版本]")
    for name, info in sorted(report["snapshots"].items()):
        add(f"  {name:<16} v{info['version']:<4} checksum={info['checksum'][:16]}...")
    trace = report["threshold_policy_trace"]
    add(f"  门限策略: {json.dumps(trace['current'], sort_keys=True)}")
    add("")

    for task in report["tasks"]:
        add(f"[任务 {task['task_id']}]  profile={task['dtype_profile']['dtype_profile']}"
            f"  grid={task['block_grid']}"
            + (f"  降级={task['degradation_category']}" if task["degraded"] else ""))
        b = task["boundary_spectrum"]
        add("  分块边界偏差谱: "
            f"max_abs={_fmt(b['boundary_max_abs_dev'])}  "
            f"max_rel={_fmt(b['boundary_max_rel_dev'])}  "
            f"frame_max_abs={_fmt(b['frame_max_abs_dev'])}  "
            f"elementwise_equal={b['elementwise_equal']}")
        g = task["boundary_gate"]
        add(f"  边界门限分类({g['strategy']}): exceed={g['exceed_count']}"
            f"/{g['count']}  flip_risk={g['flip_risk_count']}")
        add("  逐级误差谱:")
        add(f"    {'stage':<14}{'max_abs':>12}{'mean_abs':>12}{'max_rel':>12}"
            f"{'p50':>11}{'p99':>11}{'amplif':>9}")
        for name in SPECTRUM_STAGES:
            m = task["error_spectrum"][name]
            q = m["abs_dev_quantiles"]
            add(f"    {name:<14}{_fmt(m['max_abs_dev']):>12}"
                f"{_fmt(m['mean_abs_dev']):>12}{_fmt(m['max_rel_dev']):>12}"
                f"{_fmt(q.get('p50')):>11}{_fmt(q.get('p99')):>11}"
                f"{_fmt(m['amplification_ratio']):>9}")
        att = task["attribution"]
        scores = att["source_scores"]
        add(f"  主导误差来源: {att['dominant_source']}  "
            f"(boundary={_fmt(scores['chunk_boundary'])}, "
            f"quant={_fmt(scores['quantization'])}, "
            f"norm={_fmt(scores['normalization'])})")
        add("")

    if report["degradations"]:
        add("[降级记录]")
        for d in report["degradations"]:
            add(f"  task={d['task_id']}  category={d['category']}  {d['reason']}")
        add("")

    if report["injections_applied"]:
        add("[注入参数]")
        for inj in report["injections_applied"]:
            add(f"  task={inj['task_id']}  "
                f"overrides={json.dumps(inj['overrides'], sort_keys=True)}")
        add("")
    if report["error_records"]:
        add("[异常归因记录]")
        for rec in report["error_records"]:
            add(f"  task={rec['task_id']:<14} stage={rec['stage']:<16} "
                f"code={rec['code']}")
        add("")
    if report["unattributed_errors"]:
        add(f"[未归因异常] 共 {len(report['unattributed_errors'])} 条（已汇总）")
        add("")

    add(f"[最近业务错误码] {report['recent_error_codes'] or '（无）'}")
    return "\n".join(lines)


def render_from_file(report_path) -> str:
    with open(report_path, "r", encoding="utf-8") as fh:
        report = json.load(fh)
    return render_report(report)
