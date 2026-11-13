"""目录扫描：文件发现、单文件解析、稳定排序、跳过文件标注。"""
from __future__ import annotations

import ast
import pathlib

from .model import Finding
from .rules import ALL_RULE_CHECKS


def _rel_posix(path: pathlib.Path, root: pathlib.Path) -> str:
    """相对路径统一用正斜杠，保证跨平台字典序与输出稳定。"""
    return path.relative_to(root).as_posix()


def scan_file(path: pathlib.Path, rel_path: str) -> list[Finding]:
    """解析并检查单个文件；语法/编码错误向上抛出由调用方处理。"""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=rel_path)
    findings: list[Finding] = []
    for check in ALL_RULE_CHECKS:
        findings.extend(check(tree, rel_path))
    return findings


def scan_directory(root: str | pathlib.Path) -> tuple[list[Finding], list[dict[str, str]], list[str]]:
    """扫描目录内全部 .py 文件（仅单文件分析）。

    返回 (问题列表, 被跳过文件列表, 已扫描文件相对路径列表)。
    目录不存在或没有 .py 文件时三个结果均为空列表形态。
    """
    root_path = pathlib.Path(root)
    if not root_path.is_dir():
        return [], [], []

    py_files = sorted(
        (p for p in root_path.rglob("*.py") if p.is_file()),
        key=lambda p: _rel_posix(p, root_path),
    )
    if not py_files:
        return [], [], []

    findings: list[Finding] = []
    skipped: list[dict[str, str]] = []
    scanned: list[str] = []
    for py_file in py_files:
        rel = _rel_posix(py_file, root_path)
        try:
            findings.extend(scan_file(py_file, rel))
        except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
            # 单个文件语法错误不影响其余文件
            skipped.append({
                "file": rel,
                "reason": f"{type(exc).__name__}: {exc.msg if isinstance(exc, SyntaxError) else str(exc)}",
            })
            continue
        scanned.append(rel)

    # 稳定排序：文件相对路径字典序 -> 行号升序 -> 规则标识 -> 说明
    findings.sort(key=lambda f: (f.file, f.line, f.rule_id, f.message))
    skipped.sort(key=lambda s: s["file"])
    return findings, skipped, scanned


def build_report(root: str | pathlib.Path) -> dict[str, object]:
    """构造扫描报告字典（JSON 可序列化、键顺序固定）。"""
    findings, skipped, scanned = scan_directory(root)
    report: dict[str, object] = {
        "scan_root": str(root),
        "scanned_files": scanned,
        "scanned_count": len(scanned),
        "skipped_files": skipped,
        "issue_count": len(findings),
        "issues": [f.to_dict() for f in findings],
    }
    if not scanned and not skipped:
        report["notice"] = "未发现可扫描目标：目录不存在或目录内没有 .py 文件"
    return report
