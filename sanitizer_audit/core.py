"""审计核心：扫描日志、调用两套脱敏实现、汇总残留与差异。

设计要点
--------
* 每个文件独立处理，单个文件编码异常或某套实现不可用，不影响其余文件；
* 两套实现由模块名动态加载，调用失败时按 ``max_attempts`` 重试；
* 记录按相对路径字典序排列，除 ``generated_at`` 外两次运行结果完全一致；
* 所有报告内容均为纯数据（字符串 / 数字 / 布尔 / 列表 / 字典），可直接
  序列化为 UTF-8 JSON。
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from sanitizers.common import (
    contains_number,
    core_digits,
    detect_residuals,
    find_sensitive,
    residual_entries,
)

DEFAULT_LEGACY_MODULE = "sanitizers.sanitizer_legacy"
DEFAULT_FIXED_MODULE = "sanitizers.sanitizer_fixed"

STATUS_ENCODING_ERROR = "encoding_error"
STATUS_PARSE_ERROR = "parse_error"
STATUS_UNVERIFIED = "unverified"
STATUS_CONFIRMED = "confirmed_vulnerability"
STATUS_FIX_INCOMPLETE = "fix_incomplete"
STATUS_FIX_UNVERIFIED = "fix_unverified"
STATUS_CLEAN = "clean"


class AuditError(Exception):
    """审计流程中的可预期错误。"""


class LogDirectoryMissing(AuditError):
    """指定的日志目录不存在。"""


class NoLogFiles(AuditError):
    """目录下没有 .log 文件。"""


class SanitizerUnavailable(AuditError):
    """某套脱敏实现在重试后仍不可用。"""

    def __init__(self, message: str, attempts: int = 0) -> None:
        super().__init__(message)
        self.attempts = attempts


@dataclass
class FaultPlan:
    """故障注入配置，用于演示"实现暂时 / 持续不可用"。

    fail_fixed_call: 第 N 次（从 1 开始，跨文件累计）调用修复后实现时抛出
        一次异常，模拟瞬时故障；该次号码已消耗，重试时不会再触发，因此
        "首次失败、重试成功"的路径可以被真实走到。
    """

    legacy_available: bool = True
    fixed_available: bool = True
    fail_fixed_call: int | None = None


@dataclass
class SanitizerRunner:
    """封装一套脱敏实现，负责调用与重试。"""

    name: str
    function: Callable[[str], str] | None
    available: bool
    max_attempts: int
    fail_on_call: int | None = None
    calls: int = 0
    load_error: str | None = None

    def run(self, text: str) -> tuple[str, int]:
        """返回 (脱敏结果, 本次文件实际尝试次数)。

        不可用或重试耗尽时抛 SanitizerUnavailable，异常带 ``attempts``。
        """
        if not self.available or self.function is None:
            reason = self.load_error or f"{self.name} 实现已禁用"
            raise SanitizerUnavailable(reason, attempts=0)

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            self.calls += 1
            if self.fail_on_call is not None and self.calls == self.fail_on_call:
                last_error = RuntimeError(
                    f"{self.name} 实现第 {self.calls} 次调用暂时不可用（模拟故障）"
                )
                continue
            try:
                return self.function(text), attempt
            except Exception as exc:  # noqa: BLE001 - 任意实现异常都转为审计状态
                last_error = exc

        detail = f"{type(last_error).__name__}: {last_error}"
        raise SanitizerUnavailable(detail, attempts=self.max_attempts)


def _load_sanitize(module_name: str) -> Callable[[str], str]:
    module = importlib.import_module(module_name)
    function = getattr(module, "sanitize", None)
    if not callable(function):
        raise SanitizerUnavailable(f"{module_name} 未提供 sanitize(text) 函数")
    return function


def scan_log_files(log_dir: Path) -> list[tuple[Path, str]]:
    """递归扫描 *.log，返回 (绝对路径, POSIX 风格相对路径)，按相对路径排序。"""
    if not log_dir.exists():
        raise LogDirectoryMissing(f"日志目录不存在: {log_dir}")
    if not log_dir.is_dir():
        raise AuditError(f"日志路径不是目录: {log_dir}")

    files: list[tuple[Path, str]] = []
    for path in log_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() == ".log":
            files.append((path, path.relative_to(log_dir).as_posix()))
    if not files:
        raise NoLogFiles(f"目录中没有 .log 文件: {log_dir}")

    files.sort(key=lambda item: item[1])
    return files


def _decode_log(raw: bytes) -> tuple[str | None, str | None]:
    """严格按 UTF-8 解码；失败时返回 (None, 错误说明)。"""
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return None, f"UTF-8 解码失败: {exc}"


def _unparseable_reason(text: str) -> str | None:
    """判断内容是否无法按行解析。当前约定：含 NUL 控制字符即视为异常。"""
    if "\x00" in text:
        return "内容包含 NUL 控制字符，无法按行解析"
    return None


def _empty_record(relative_path: str, status: str, error: str) -> dict:
    return {
        "path": relative_path,
        "line_count": None,
        "status": status,
        "residuals": [],
        "legacy_residual_count": None,
        "fixed_residuals": [],
        "fixed_residual_count": None,
        "fixed_verification": "skipped",
        "leaked_numbers": [],
        "attempts": {"legacy": 0, "fixed": 0},
        "error": error,
    }


def _analyze_text(
    text: str,
    relative_path: str,
    legacy_runner: SanitizerRunner,
    fixed_runner: SanitizerRunner,
) -> dict:
    record = {
        "path": relative_path,
        "line_count": len(text.splitlines()),
        "status": STATUS_CLEAN,
        "residuals": [],
        "legacy_residual_count": None,
        "fixed_residuals": [],
        "fixed_residual_count": None,
        "fixed_verification": "skipped",
        "leaked_numbers": [],
        "attempts": {"legacy": 0, "fixed": 0},
        "error": None,
    }
    diagnostics: list[str] = []

    # ---- 修复前实现：识别残留，作为漏洞判定依据 ----
    legacy_count: int | None = None
    try:
        legacy_output, attempts = legacy_runner.run(text)
        record["attempts"]["legacy"] = attempts
        legacy_hits = detect_residuals(legacy_output)
        legacy_count = len(legacy_hits)
        record["legacy_residual_count"] = legacy_count
        record["residuals"] = residual_entries(legacy_hits)
    except SanitizerUnavailable as exc:
        record["attempts"]["legacy"] = getattr(exc, "attempts", 0)
        diagnostics.append(f"修复前实现不可用: {exc}")

    # ---- 修复后实现：核查是否仍有残留 ----
    try:
        fixed_output, attempts = fixed_runner.run(text)
        record["attempts"]["fixed"] = attempts
        fixed_hits = detect_residuals(fixed_output)
        record["fixed_residual_count"] = len(fixed_hits)
        record["fixed_residuals"] = residual_entries(fixed_hits)

        # 按号码原文检索：原文中出现的每个号码，去分隔符后不应再出现在
        # 修复后文本中。这比只数残留更能证明"输出不含原始号码"。
        leaked: set[tuple[str, int]] = set()
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in find_sensitive(line):
                if contains_number(fixed_output, core_digits(match)):
                    leaked.add((match.kind, line_number))
        record["leaked_numbers"] = [
            {"type": kind, "line": line}
            for kind, line in sorted(leaked, key=lambda item: (item[1], item[0]))
        ]

        ok = not fixed_hits and not record["leaked_numbers"]
        record["fixed_verification"] = "passed" if ok else "failed"
    except SanitizerUnavailable as exc:
        record["attempts"]["fixed"] = getattr(exc, "attempts", 0)
        record["fixed_verification"] = "unavailable"
        diagnostics.append(f"修复后实现不可用: {exc}")

    # ---- 结论 ----
    # 已确认漏洞的判定只看修复前实现的残留：即使修复后实现不可用，这个
    # 结论依然成立，不会被"未能验证"覆盖。
    if legacy_count is None:
        record["status"] = STATUS_UNVERIFIED
    elif legacy_count > 0:
        record["status"] = STATUS_CONFIRMED
    elif record["fixed_verification"] == "passed":
        record["status"] = STATUS_CLEAN
    elif record["fixed_verification"] == "failed":
        record["status"] = STATUS_FIX_INCOMPLETE
    else:  # fixed_verification == "unavailable"
        record["status"] = STATUS_FIX_UNVERIFIED

    if diagnostics:
        record["error"] = "；".join(diagnostics)
    return record


def build_report(
    log_dir: Path,
    *,
    legacy_module: str = DEFAULT_LEGACY_MODULE,
    fixed_module: str = DEFAULT_FIXED_MODULE,
    fault_plan: FaultPlan | None = None,
    max_attempts: int = 2,
) -> dict:
    """扫描目录并生成报告字典。"""
    plan = fault_plan or FaultPlan()
    files = scan_log_files(log_dir)

    try:
        legacy_function: Callable[[str], str] | None = _load_sanitize(legacy_module)
        legacy_load_error = None
    except Exception as exc:  # noqa: BLE001
        legacy_function, legacy_load_error = None, f"{type(exc).__name__}: {exc}"

    try:
        fixed_function: Callable[[str], str] | None = _load_sanitize(fixed_module)
        fixed_load_error = None
    except Exception as exc:  # noqa: BLE001
        fixed_function, fixed_load_error = None, f"{type(exc).__name__}: {exc}"

    legacy_runner = SanitizerRunner(
        name="legacy",
        function=legacy_function,
        available=plan.legacy_available,
        max_attempts=max_attempts,
        load_error=legacy_load_error,
    )
    fixed_runner = SanitizerRunner(
        name="fixed",
        function=fixed_function,
        available=plan.fixed_available,
        max_attempts=max_attempts,
        fail_on_call=plan.fail_fixed_call,
        load_error=fixed_load_error,
    )

    records: list[dict] = []
    for path, relative_path in files:
        text, decode_error = _decode_log(path.read_bytes())
        if text is None:
            records.append(
                _empty_record(relative_path, STATUS_ENCODING_ERROR, decode_error or "")
            )
            continue

        parse_error = _unparseable_reason(text)
        if parse_error is not None:
            records.append(_empty_record(relative_path, STATUS_PARSE_ERROR, parse_error))
            continue

        records.append(_analyze_text(text, relative_path, legacy_runner, fixed_runner))

    confirmed_files = [item["path"] for item in records if item["status"] == STATUS_CONFIRMED]
    # "受影响"指修复后核查未能完成的文件：包括修复前实现不可用、修复后实现
    # 不可用两种情况。已确认漏洞仍留在 confirmed_vulnerabilities 中，互不覆盖。
    affected_files = [
        item["path"]
        for item in records
        if item["fixed_verification"] == "unavailable"
        or item["status"] == STATUS_UNVERIFIED
    ]
    fixed_verified_files = sum(
        1 for item in records if item["fixed_verification"] in ("passed", "failed")
    )

    summary = {
        "total_files": len(records),
        "clean": sum(1 for item in records if item["status"] == STATUS_CLEAN),
        "confirmed_vulnerabilities": sum(
            1 for item in records if item["status"] == STATUS_CONFIRMED
        ),
        "fix_incomplete": sum(1 for item in records if item["status"] == STATUS_FIX_INCOMPLETE),
        "fix_unverified": sum(1 for item in records if item["status"] == STATUS_FIX_UNVERIFIED),
        "unverified": sum(1 for item in records if item["status"] == STATUS_UNVERIFIED),
        "encoding_errors": sum(
            1 for item in records if item["status"] == STATUS_ENCODING_ERROR
        ),
        "parse_errors": sum(1 for item in records if item["status"] == STATUS_PARSE_ERROR),
        "legacy_residual_total": sum(item["legacy_residual_count"] or 0 for item in records),
        "fixed_residual_total": sum(item["fixed_residual_count"] or 0 for item in records),
        "fixed_verified_files": fixed_verified_files,
    }

    unavailable = []
    if not plan.legacy_available or legacy_function is None:
        unavailable.append(
            {
                "name": "legacy",
                "reason": legacy_load_error or "修复前实现已禁用",
            }
        )
    if not plan.fixed_available or fixed_function is None:
        unavailable.append(
            {
                "name": "fixed",
                "reason": fixed_load_error or "修复后实现已禁用",
            }
        )

    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "log_dir": str(log_dir.resolve()),
        "implementations": {"legacy": legacy_module, "fixed": fixed_module},
        "summary": summary,
        "confirmed_vulnerabilities": confirmed_files,
        "incomplete": {
            "unavailable_implementations": unavailable,
            "affected_files": affected_files,
            "fixed_verified_files": fixed_verified_files,
            "note": (
                "受影响文件未能完成修复后实现核查；已确认漏洞列表基于修复前实现的"
                "结果，两者分开呈现。"
                if affected_files
                else ""
            ),
        },
        "records": records,
    }
    return report


def write_report(report: dict, report_path: Path) -> Path:
    """以 UTF-8 写出 JSON，父目录不存在时自动创建。"""
    report_path = report_path.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report_path
