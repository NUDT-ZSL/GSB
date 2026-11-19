"""结果报告的规范化序列化与复现哈希。

哈希口径：张量按 float64 原始字节（C 连续）进入 SHA-256，
诊断数据按 sort_keys 的规范化 JSON 进入。同一 case + 同一 config +
固定 seed 的两次运行必须产出相同哈希。
"""

from __future__ import annotations

import hashlib
import json

import numpy as np


def _arr_bytes(a):
    if a is None:
        return b""
    return np.ascontiguousarray(a, dtype=np.float64).tobytes()


def result_digest(result):
    """单 case 完整产出的规范化字典（可 JSON 序列化、键序确定）。"""
    return {
        "case_id": result.case_id,
        "status": result.status,
        "config": result.config.to_dict(),
        "case": {k: result.case[k] for k in sorted(result.case)},
        "angle_diag": result.angle_diag,
        "max_block_dev": result.max_block_dev,
        "injections": result.injections,
        "degradations": result.degradations,
        "errors": result.errors,
        "unattributed": result.unattributed,
        "executed_stages": result.executed_stages,
        "resources": result.resources,
        "norm_stats": result.norm_stats,
    }


def result_hash(result):
    """完整输出哈希：张量字节 + 规范化诊断 JSON 的 SHA-256。"""
    h = hashlib.sha256()
    for arr in (result.angles, result.q_rot, result.k_rot,
                result.output, result.full_output):
        h.update(_arr_bytes(arr))
    diag = json.dumps(result_digest(result), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))
    h.update(diag.encode("utf-8"))
    return h.hexdigest()


def tensor_hash(arr):
    return hashlib.sha256(_arr_bytes(arr)).hexdigest()


def run_report(results, variant_reports=None, verify_issues=None):
    """汇总一次完整运行的报告（含每 case 哈希与整体哈希）。"""
    cases = []
    for r in results:
        cases.append({
            "case_id": r.case_id,
            "status": r.status,
            "hash": result_hash(r),
            "max_block_dev": r.max_block_dev,
            "injections": len(r.injections),
            "degradations": len(r.degradations),
            "errors": [e["code"] for e in r.errors],
            "unattributed": len(r.unattributed),
        })
    report = {
        "cases": cases,
        "variant_reports": variant_reports or [],
        "verify_issues": verify_issues or [],
    }
    blob = json.dumps(report, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    report["run_hash"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return report
