"""修复对比模式（任务第 7、8 条）。

对修复后接口用同一批参数、同一套载荷复测，并与修复前报告逐项对照：
- 原可疑项不再触发 → fixed；
- 仍可触发 → remains_vulnerable，并进入单独的残留清单，不被已修复项掩盖；
- 复测未完成 → retest_incomplete。
"""

from __future__ import annotations

from .config import TargetConfig
from .report import TIME_PLACEHOLDER, canonical_json
from .scanner import ScanOutcome
from pathlib import Path
import json

_CONCLUSION_TEXT = {
    "fixed": "已修复：修复前可疑/已确认，修复后同批载荷不再触发",
    "remains_vulnerable": "残留风险：修复后仍可触发注入，未被修复覆盖",
    "retest_incomplete": "复测未完成：服务故障导致无法对修复效果下结论",
    "previously_clean": "修复前后均未发现可疑",
}


def build_comparison(cfg: TargetConfig, before: dict, after_outcome: ScanOutcome) -> dict:
    before_map = {r["name"]: r for r in before["parameters"]}
    after_map = {r["name"]: r for r in after_outcome.param_records}

    comparisons: list[dict] = []
    residual: list[dict] = []

    # 以并集、固定 (name, location) 顺序遍历，保证输出确定
    all_names = sorted(set(before_map) | set(after_map))
    for name in all_names:
        b = before_map.get(name)
        a = after_map.get(name)
        before_status = b["status"] if b else "not_scanned"
        after_status = a["status"] if a else "not_scanned"
        before_suspicious = bool(b and b["suspicious"])
        after_suspicious = bool(a and a["suspicious"])

        if after_status == "incomplete":
            conclusion = "retest_incomplete"
        elif before_suspicious and not after_suspicious:
            conclusion = "fixed"
        elif after_suspicious:
            conclusion = "remains_vulnerable"
        else:
            conclusion = "previously_clean"

        entry = {
            "name": name,
            "location": (a or b)["location"],
            "before_status": before_status,
            "before_confirmation": b["confirmation_status"] if b else "not_applicable",
            "after_status": after_status,
            "after_confirmation": a["confirmation_status"] if a else "not_applicable",
            "conclusion": conclusion,
            "conclusion_text": _CONCLUSION_TEXT[conclusion],
            "before_evidence": b["evidence_summary"] if b else "",
            "after_evidence": a["evidence_summary"] if a else "",
        }
        comparisons.append(entry)
        if conclusion == "remains_vulnerable":
            residual.append({
                "name": name,
                "location": entry["location"],
                "after_status": after_status,
                "after_confirmation": entry["after_confirmation"],
                "evidence_summary": a["evidence_summary"],
                "suspicious_probe_ids": list(a["suspicious_probe_ids"]),
            })

    s = after_outcome.summary
    residual_names = [r["name"] for r in residual]
    if residual:
        business = (
            f"修复对照结论：仍有 {len(residual)} 个参数残留注入风险（{ '、'.join(residual_names) }），"
            "修复不完整，需继续整改；已修复参数的结论见对照表。"
        )
    elif not s["coverage_complete"]:
        business = (
            "修复对照结论：已完成复测的原可疑参数均不再触发，但存在未完成复测项，"
            "当前不能给出“全部修复”的结论，请恢复服务后重跑。"
        )
    else:
        business = "修复对照结论：修复前全部可疑参数在同批载荷复测下均不再触发，本批样本未发现残留注入。"

    return {
        "report_schema_version": before["report_schema_version"],
        "generated_at": TIME_PLACEHOLDER,
        "elapsed": TIME_PLACEHOLDER,
        "run_kind": "remediation_comparison",
        "target": cfg.to_report_dict(),
        "before_report_target": before["target"]["target_name"],
        "after_scan_summary": dict(s),
        "comparisons": comparisons,
        "residual_vulnerable_params": residual,
        "business_summary": business,
    }


def write_comparison(report: dict, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(canonical_json(report), encoding="utf-8", newline="\n")
    return p


def load_report(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
