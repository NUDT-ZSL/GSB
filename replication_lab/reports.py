"""Stable human-readable and JSON report rendering."""

from __future__ import annotations

from typing import Any, Mapping

from .util import stable_json


def _fmt_ms(value: Any) -> str:
    return "未知" if value is None else f"{value} ms"


def render_observation_report(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append("主从复制延迟观测报告")
    lines.append("=" * 36)
    lines.append(f"版本集合: {report.get('format_version_set', [])}")
    lines.append(
        f"日志段: {report.get('segment_count', 0)} 个，记录: {report.get('record_count', 0)} 条"
    )
    for segment in report.get("segments", []):
        lines.append(
            f"  - {segment['filename']} id={segment['segment_id']} "
            f"段版本={segment['format_version']} 记录版本={segment['versions']} "
            f"记录数={segment['record_count']} sha256={segment['digest_short']}"
        )
    replay = report.get("replay", {})
    state = replay.get("state")
    if state is None:
        lines.append("回放: 尚无持久化进度")
    else:
        lines.append(
            f"回放参数: rate={state['replay_rate']} txn/s, "
            f"concurrency={state['concurrency']}, origin={state['clock_origin_ms']} ms, "
            f"参数版本={state['params_version']}, 估算参数版本={state['estimator_version']}"
        )
        lines.append(
            f"回放进度: last_commit_seq={state['last_commit_seq']} "
            f"已完成批次={sum(1 for b in replay.get('batches', []) if b['status'] == 'COMPLETE')} "
            f"未知批次={state['unknown_batch_count']}"
        )
    lines.append(
        f"样本: total={report.get('sample_count', 0)} "
        f"known={report.get('known_delay_count', 0)} "
        f"unknown_delay={report.get('unknown_delay_count', 0)}"
    )
    estimate = report.get("estimation", {})
    lines.append(
        f"估算: mean_fixed={estimate.get('mean_fixed')} "
        f"(Q={estimate.get('mean_fixed_scale')}) "
        f"mean={estimate.get('mean_ms')} ms "
        f"P95={_fmt_ms(estimate.get('p95_ms'))} "
        f"max={_fmt_ms(estimate.get('max_delay_ms'))}"
    )
    lines.append(
        f"状态: {estimate.get('estimate_status')} 置信: {estimate.get('confidence')} "
        f"原因: {estimate.get('status_reason')}"
    )
    sensitivity = estimate.get("parameter_sensitivity")
    if sensitivity:
        lines.append(
            f"调度误差: batch_duration={sensitivity['batch_duration_ms']} ms, "
            f"batch_count={sensitivity['batch_count']}, "
            f"schedule_bound={estimate.get('schedule_error_upper_bound_ms')} ms"
        )
    bound = estimate.get("total_estimate_error_upper_bound")
    if bound:
        lines.append(f"总误差保守上界: {bound['text']}")
    lines.append("误差界推导:")
    for index, step in enumerate(estimate.get("error_bound_steps", []), 1):
        lines.append(f"  {index}. {step}")
    lines.append(f"结论: {report.get('conclusion')}")
    upgrade = report.get("upgrade_report")
    if upgrade:
        lines.append(
            f"升级: before={upgrade.get('version_set_before')} "
            f"after={upgrade.get('version_set_after')} "
            f"upgraded={len(upgrade.get('upgraded', []))} "
            f"skipped={len(upgrade.get('skipped', []))} "
            f"failed={len(upgrade.get('failed', []))} "
            f"business_code={upgrade.get('business_code')}"
        )
    if report.get("format_errors"):
        lines.append("格式错误:")
        for error in report["format_errors"]:
            lines.append(
                f"  - {error.get('business_code')} segment={error.get('segment')} "
                f"offset={error.get('offset')} {error.get('message')}"
            )
    return "\n".join(lines) + "\n"


def render_recovery_report(report: Mapping[str, Any]) -> str:
    return (
        "强制终止恢复报告\n"
        "----------------\n"
        f"已恢复提交序号: {report.get('recovered_last_commit_seq')}\n"
        f"未知批次: {report.get('unknown_batch_count')} {report.get('unknown_batch_ids')}\n"
        f"重建样本数: {report.get('rebuilt_sample_count')}\n"
        f"已知/未知延迟样本: {report.get('known_delay_count')}/{report.get('unknown_delay_count')}\n"
        f"是否声称退出报告: {report.get('claim_exit_report')}\n"
        f"说明: {report.get('note')}\n"
    )


def render_verification_report(report: Mapping[str, Any]) -> str:
    lines = [
        "历史观测重放校验报告",
        "--------------------",
        f"快照: {report.get('snapshot_id')}",
        f"业务码: {report.get('business_code')}",
        f"结果: {'可复现' if report.get('ok') else '不可复现'}",
    ]
    for comparison in report.get("comparisons", []):
        marker = "OK" if comparison["match"] else "MISMATCH"
        lines.append(
            f"  [{marker}] {comparison['item']}: expected={comparison['expected']!r} "
            f"actual={comparison['actual']!r}"
        )
    if report.get("mismatch_items"):
        lines.append("不一致项: " + ", ".join(report["mismatch_items"]))
    return "\n".join(lines) + "\n"


def render_json_report(report: Mapping[str, Any]) -> str:
    return stable_json(report) + "\n"

