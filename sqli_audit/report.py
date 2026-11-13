"""确定性报告输出（任务第 6、11、12 条）。

- JSON 键序固定、缩进固定、无时间/耗时字段（使用固定占位值），
  同一输入重复运行产出逐字节一致的报告；
- 另输出面向业务人员的纯文本摘要，存在未完成项时明确提示覆盖不全，
  绝不把未覆盖当作“安全”。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import REPORT_SCHEMA_VERSION
from .config import TargetConfig
from .scanner import ScanOutcome

# 时间/耗时统一占位符：报告不包含任何时钟读数，保证可复现
TIME_PLACEHOLDER = "OMITTED_FOR_REPRODUCIBILITY"

_STATUS_TEXT = {
    "vulnerable_confirmed": "已确认注入",
    "suspicious_unconfirmed": "可疑但二次确认未成立",
    "not_suspicious": "未发现可疑",
    "incomplete": "存在未完成探测",
}


def build_report(cfg: TargetConfig, outcome: ScanOutcome, run_kind: str = "single") -> dict:
    s = outcome.summary
    business_lines: list[str] = []
    business_lines.append(f"【{cfg.target_name}】SQL 注入自查业务摘要（运行类型：{run_kind}）")
    business_lines.append(
        f"本次覆盖参数 {s['params_total']} 个，初筛探测 {s['probes_completed']}/{s['probes_total']} 项完成，"
        f"二次确认 {s['confirmations_completed']}/{s['confirmations_total']} 项完成。"
    )

    confirmed = [r for r in outcome.param_records if r["status"] == "vulnerable_confirmed"]
    suspicious = [r for r in outcome.param_records if r["status"] == "suspicious_unconfirmed"]
    incomplete = [r for r in outcome.param_records if r["status"] == "incomplete"]
    clean = [r for r in outcome.param_records if r["status"] == "not_suspicious"]

    if confirmed:
        names = "、".join(r["name"] for r in confirmed)
        business_lines.append(f"可用结论：参数 {names} 已确认存在 SQL 注入，可越权读取订单数据，需立即整改。")
    elif suspicious:
        names = "、".join(r["name"] for r in suspicious)
        business_lines.append(f"可用结论：参数 {names} 初筛可疑但确认证据不足，建议人工复核。")
    elif clean and not incomplete:
        business_lines.append("可用结论：已覆盖参数在本批确定性载荷下未发现 SQL 注入迹象。")

    if clean:
        business_lines.append(
            "未发现可疑的参数：" + "、".join(r["name"] for r in clean) +
            "（结论仅对本次载荷集合与样本数据有效）。"
        )
    if incomplete:
        detail = "；".join(
            f"{r['name']}（{'；'.join(r['incomplete_reasons'])}）" for r in incomplete
        )
        business_lines.append(
            "覆盖不全提示：以下参数存在未完成探测，本次扫描不能对其给出安全结论，"
            f"服务恢复后请重跑补齐：{detail}。"
        )
        business_lines.append("注意：未覆盖不等于安全，请勿依据本次结果宣称全部参数无风险。")
    else:
        business_lines.append("本次探测覆盖完整，但结论仍以确定性样本与固定载荷为限，不替代全面安全评估。")

    report = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": TIME_PLACEHOLDER,
        "elapsed": TIME_PLACEHOLDER,
        "run_kind": run_kind,
        "target": cfg.to_report_dict(),
        "scan_summary": dict(s),
        "baseline": {
            "request_fields": dict(cfg.baseline),
            "fingerprint": outcome.baseline_fingerprint.to_dict(),
        },
        "parameters": outcome.param_records,
        "business_summary": "\n".join(business_lines),
    }
    return report


def canonical_json(report: dict) -> str:
    """逐字节稳定的序列化：sort_keys + 固定缩进 + 固定换行。"""
    return json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def write_report(report: dict, report_path: str | Path) -> Path:
    p = Path(report_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(canonical_json(report), encoding="utf-8", newline="\n")
    return p


def write_text_summary(report: dict, summary_path: str | Path) -> Path:
    p = Path(summary_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(report["business_summary"] + "\n", encoding="utf-8", newline="\n")
    return p
