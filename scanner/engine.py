"""扫描引擎：读取样本清单、逐样本检测、组装确定性报告。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import SCANNER_VERSION
from .detector import detect
from .models import (
    CONCLUSION_CLEAN,
    CONCLUSION_CONFIRMED,
    CONCLUSION_INCOMPLETE,
    CONCLUSION_SUSPECTED,
    Finding,
    SampleResult,
    SEVERITY_ORDER,
)

# 样本处理状态
SAMPLE_DONE = "完成"
SAMPLE_INCOMPLETE = "未完成"

MANIFEST_FILENAME = "manifest.json"

# 样本在清单中声明的纳入状态
DECLARED_INCLUDED = "included"
DECLARED_EXCLUDED = "excluded"


class ManifestError(Exception):
    """清单文件缺失或本身不可解析时抛出。"""


def _finding_sort_key(finding: Finding) -> tuple[Any, ...]:
    """同一报告内的固定排序：级别高→低，已确认优先于可疑，再按类别与位置。"""

    return (
        SEVERITY_ORDER[finding.severity],
        0 if finding.conclusion == CONCLUSION_CONFIRMED else 1,
        finding.category,
        finding.location,
        finding.field_name or "",
    )


def load_manifest(scan_root: Path) -> list[dict[str, str]]:
    """加载并校验样本清单。只返回声明纳入检测的条目。"""

    manifest_path = scan_root / MANIFEST_FILENAME
    try:
        raw = manifest_path.read_bytes()
    except FileNotFoundError as exc:
        raise ManifestError(f"清单文件不存在：{manifest_path}") from exc
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"清单文件不可解析：{manifest_path}，原因：{exc}") from exc

    if not isinstance(manifest, dict) or not isinstance(manifest.get("samples"), list):
        raise ManifestError("清单结构无效：顶层必须包含 samples 数组。")

    declared: list[dict[str, str]] = []
    for index, entry in enumerate(manifest["samples"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("sample_id"), str):
            raise ManifestError(f"清单第 {index} 条样本声明结构无效。")
        if entry.get("declaration", DECLARED_INCLUDED) == DECLARED_INCLUDED:
            declared.append(
                {
                    "sample_id": entry["sample_id"],
                    "sample_file": str(entry.get("sample_file", "")),
                }
            )
    # 清单本身确定性生成；额外按 sample_id 排序兜底，保证遍历顺序固定
    declared.sort(key=lambda e: e["sample_id"])
    return declared


def _scan_one(scan_root: Path, sample_id: str, sample_file: str) -> SampleResult:
    """检测单个样本。任何读取/解析失败都转为“未完成”，不抛出。"""

    path = scan_root / sample_file
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return SampleResult(
            sample_id=sample_id,
            sample_file=sample_file,
            status=SAMPLE_INCOMPLETE,
            conclusion=CONCLUSION_INCOMPLETE,
            error=f"样本文件缺失：{sample_file}",
            findings=[],
        )
    except OSError as exc:
        return SampleResult(
            sample_id=sample_id,
            sample_file=sample_file,
            status=SAMPLE_INCOMPLETE,
            conclusion=CONCLUSION_INCOMPLETE,
            error=f"样本文件不可读：{exc.__class__.__name__}: {exc}",
            findings=[],
        )

    try:
        sample = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return SampleResult(
            sample_id=sample_id,
            sample_file=sample_file,
            status=SAMPLE_INCOMPLETE,
            conclusion=CONCLUSION_INCOMPLETE,
            error=f"样本内容不可解析：{exc.__class__.__name__}: {exc}",
            findings=[],
        )

    if not isinstance(sample, dict) or not isinstance(sample.get("response"), dict):
        return SampleResult(
            sample_id=sample_id,
            sample_file=sample_file,
            status=SAMPLE_INCOMPLETE,
            conclusion=CONCLUSION_INCOMPLETE,
            error="样本结构无效：缺少 response 对象。",
            findings=[],
        )

    response = sample["response"]
    headers = response.get("headers") or {}
    if not isinstance(headers, dict):
        return SampleResult(
            sample_id=sample_id,
            sample_file=sample_file,
            status=SAMPLE_INCOMPLETE,
            conclusion=CONCLUSION_INCOMPLETE,
            error="样本结构无效：response.headers 必须为对象。",
            findings=[],
        )
    headers = {str(k): ("" if v is None else str(v)) for k, v in headers.items()}

    body_value = response.get("body", "")
    if isinstance(body_value, (dict, list)):
        parsed_body: object = body_value
        body_text = json.dumps(body_value, ensure_ascii=False, sort_keys=True)
    elif isinstance(body_value, str):
        parsed_body = _try_parse_json_text(body_value)
        body_text = body_value
    else:
        body_text = str(body_value)
        parsed_body = None

    findings = detect(headers, body_text, parsed_body)
    findings.sort(key=_finding_sort_key)
    for index, finding in enumerate(findings, start=1):
        # dataclass 为 frozen，用 object.__setattr__ 注入确定性编号
        object.__setattr__(finding, "finding_id", f"{sample_id}-F{index:02d}")

    if not findings:
        conclusion = CONCLUSION_CLEAN
    elif any(f.conclusion == CONCLUSION_CONFIRMED for f in findings):
        conclusion = CONCLUSION_CONFIRMED
    else:
        conclusion = CONCLUSION_SUSPECTED

    return SampleResult(
        sample_id=sample_id,
        sample_file=sample_file,
        status=SAMPLE_DONE,
        conclusion=conclusion,
        error=None,
        findings=findings,
    )


def _try_parse_json_text(text: str) -> object:
    """正文为字符串且本身是 JSON 时解析为结构，否则返回 None。"""

    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return None
    return None


def scan(scan_root: Path) -> dict[str, Any]:
    """执行完整扫描并返回可直接序列化的报告字典。"""

    declared = load_manifest(scan_root)
    results = [_scan_one(scan_root, entry["sample_id"], entry["sample_file"]) for entry in declared]

    completed = [r for r in results if r.status == SAMPLE_DONE]
    incomplete = [r for r in results if r.status == SAMPLE_INCOMPLETE]
    confirmed_count = sum(
        1 for r in completed if r.conclusion == CONCLUSION_CONFIRMED
    )
    suspected_count = sum(1 for r in completed if r.conclusion == CONCLUSION_SUSPECTED)
    clean_count = sum(1 for r in completed if r.conclusion == CONCLUSION_CLEAN)

    try:
        scan_root_display = os.path.relpath(scan_root, Path.cwd())
    except ValueError:
        # Windows 跨盘符无法计算相对路径时回退为绝对路径
        scan_root_display = str(scan_root)
    report = {
        "scanner_version": SCANNER_VERSION,
        "scan_root": scan_root_display,
        "summary": {
            "declared_samples": len(declared),
            "completed_samples": len(completed),
            "incomplete_samples": len(incomplete),
            "confirmed_samples": confirmed_count,
            "suspected_samples": suspected_count,
            "clean_samples": clean_count,
            "total_findings": sum(len(r.findings) for r in results),
        },
        "samples": [r.to_dict() for r in results],
    }
    return report
