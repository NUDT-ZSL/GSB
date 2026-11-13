"""扫描编排：逐目标、逐注入点执行检测并组装报告。

设计要点：
- 目标预检失败（拒绝/超时）只影响该目标，其余目标照常完成（需求 4）
- 单个路径 5xx 记为警告并继续（需求 8）
- 取消信号在目标/注入点边界检查，已完成结果落盘，未处理目标标注未扫描（需求 9）
- 漏洞按严重性降序、同目标同类型同参数去重（需求 2、15）
"""

from __future__ import annotations

import datetime as _dt
import json
import os

from .checks import Finding, build_probe_url, analyze, CHECK_METADATA
from .config import (
    TargetSpec, Policy,
    ALL_CHECKS, SEVERITY_ORDER, SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW,
)
from .http_client import http_get, TargetUnreachable, RequestTimeout

# 目标扫描状态
STATUS_COMPLETED = "completed"
STATUS_UNREACHABLE = "unreachable"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"
STATUS_NOT_SCANNED = "not_scanned"


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings,
                  key=lambda f: (SEVERITY_ORDER[f.severity], f.type, f.url,
                                 f.parameter or ""))


def _finding_jsonl_line(target_url: str, finding: Finding) -> str:
    return json.dumps({
        "target_url": target_url,
        "vulnerability_type": finding.type,
        "vulnerability_type_label": finding.type_label,
        "url": finding.url,
        "parameter": finding.parameter,
        "severity": finding.severity,
        "remediation": finding.remediation,
    }, ensure_ascii=False)


def _disabled_checks(policy: Policy) -> list[str]:
    return [c for c in ALL_CHECKS if c not in policy.enabled_checks]


def _skip_entries(checks: list[str], reason: str) -> list[dict]:
    return [{"check": c, "label": CHECK_METADATA[c]["label"], "reason": reason}
            for c in checks]


def scan(targets: list[TargetSpec], policy: Policy, *,
         record_path: str, cancel_file: str | None = None) -> dict:
    """执行扫描并返回报告 dict；同时把命中明细增量写入 JSONL 记录文件。"""
    started_at = _dt.datetime.now().isoformat(timespec="seconds")
    target_results: list[dict] = []
    cancelled = False

    with open(record_path, "w", encoding="utf-8") as record_handle:
        for target in targets:
            # 取消信号：目标边界检查，尚未开始的目标整体标注未扫描
            if cancel_file and os.path.exists(cancel_file):
                cancelled = True
            if cancelled:
                target_results.append(_not_scanned_result(target, policy))
                continue

            result = _scan_target(target, policy, record_handle, cancel_file)
            target_results.append(result)
            if result["scan_status"] == STATUS_CANCELLED:
                cancelled = True

    finished_at = _dt.datetime.now().isoformat(timespec="seconds")
    return {
        "report_version": "1.0",
        "started_at": started_at,
        "finished_at": finished_at,
        "cancelled": cancelled,
        "targets": target_results,
        "summary": _build_summary(target_results),
    }


def _not_scanned_result(target: TargetSpec, policy: Policy) -> dict:
    return {
        "name": target.name,
        "target_url": target.base_url,
        "scan_completed": False,
        "scan_status": STATUS_NOT_SCANNED,
        "status_detail": "扫描中途取消，该目标尚未处理",
        "findings": [],
        "warnings": [],
        "skipped_checks": _skip_entries(
            _disabled_checks(policy), "扫描取消，未执行"),
    }


def _scan_target(target: TargetSpec, policy: Policy,
                 record_handle, cancel_file: str | None) -> dict:
    # 预检：等待上限内无响应或连接被拒绝 -> 整目标未完成（需求 4）
    probe_url = target.base_url.rstrip("/") + "/health"
    try:
        http_get(probe_url, policy.timeout_seconds)
    except RequestTimeout:
        return {
            "name": target.name,
            "target_url": target.base_url,
            "scan_completed": False,
            "scan_status": STATUS_TIMEOUT,
            "status_detail": "目标在 %.1fs 等待上限内无响应: %s"
                             % (policy.timeout_seconds, probe_url),
            "findings": [],
            "warnings": [],
            "skipped_checks": _skip_entries(
                _disabled_checks(policy), "目标无响应，未执行检测"),
        }
    except TargetUnreachable as exc:
        return {
            "name": target.name,
            "target_url": target.base_url,
            "scan_completed": False,
            "scan_status": STATUS_UNREACHABLE,
            "status_detail": "连接被拒绝或目标不可达: %s" % exc,
            "findings": [],
            "warnings": [],
            "skipped_checks": _skip_entries(
                _disabled_checks(policy), "目标不可达，未执行检测"),
        }

    result = {
        "name": target.name,
        "target_url": target.base_url,
        "scan_completed": True,
        "scan_status": STATUS_COMPLETED,
        "status_detail": None,
        "findings": [],
        "warnings": [],
        "skipped_checks": [],
    }
    seen: set[tuple] = set()
    findings: list[Finding] = []
    skipped: list[dict] = []

    for endpoint in target.endpoints:
        if cancel_file and os.path.exists(cancel_file):
            # 中途取消：已得到的结果保留，剩余注入点/目标不再处理（需求 9）
            result["scan_completed"] = False
            result["scan_status"] = STATUS_CANCELLED
            result["status_detail"] = "扫描中途被取消，已完成的注入点结果保留"
            break

        for check in endpoint.checks:
            if check not in policy.enabled_checks:  # 策略排除：标注但不检测（需求 7）
                skipped.append({
                    "check": check,
                    "label": CHECK_METADATA[check]["label"],
                    "path": endpoint.path,
                    "parameter": endpoint.parameter,
                    "reason": "扫描策略已禁用该检测项",
                })
                continue

            url = build_probe_url(target.base_url, endpoint.path, check,
                                  endpoint.parameter, endpoint.placeholder)
            try:
                resp = http_get(url, policy.timeout_seconds)
            except RequestTimeout:
                result["warnings"].append({
                    "path": endpoint.path, "url": url,
                    "message": "等待 %.1fs 上限内无响应，跳过该注入点"
                               % policy.timeout_seconds,
                })
                continue
            except TargetUnreachable as exc:
                # 扫描过程中目标失联：目标整体标记未完成，保留此前已完成结果
                result["scan_completed"] = False
                result["scan_status"] = STATUS_UNREACHABLE
                result["status_detail"] = "连接失败: %s" % exc
                result["findings"] = [f.to_dict()
                                      for f in sort_findings(findings)]
                result["skipped_checks"] = skipped
                return result

            if resp.status >= 500:  # 异常状态：警告并继续其余路径（需求 8）
                result["warnings"].append({
                    "path": endpoint.path, "url": url,
                    "message": "目标路径返回异常 HTTP 状态 %d，已记录并继续"
                               % resp.status,
                })
                continue

            finding = analyze(check, url, endpoint.parameter, resp)
            if finding and finding.key() not in seen:  # 去重（需求 15）
                seen.add(finding.key())
                findings.append(finding)
                record_handle.write(
                    _finding_jsonl_line(target.base_url, finding) + "\n")
                record_handle.flush()

    result["findings"] = [f.to_dict() for f in sort_findings(findings)]
    result["skipped_checks"] = skipped
    return result


def _build_summary(target_results: list[dict]) -> dict:
    counts = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 0, SEVERITY_LOW: 0}
    completed = 0
    incomplete = 0
    for result in target_results:
        if result["scan_completed"]:
            completed += 1
        else:
            incomplete += 1
        for finding in result["findings"]:
            counts[finding["severity"]] += 1
    return {
        "total_targets": len(target_results),
        "completed_targets": completed,
        "incomplete_targets": incomplete,
        "vulnerabilities_by_severity": [
            {"severity": SEVERITY_HIGH, "label": "高", "count": counts[SEVERITY_HIGH]},
            {"severity": SEVERITY_MEDIUM, "label": "中", "count": counts[SEVERITY_MEDIUM]},
            {"severity": SEVERITY_LOW, "label": "低", "count": counts[SEVERITY_LOW]},
        ],
        "total_vulnerabilities": sum(counts.values()),
        "total_warnings": sum(len(r["warnings"]) for r in target_results),
    }


def write_report(report: dict, path: str) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
