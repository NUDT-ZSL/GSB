"""扫描器核心：遍历授权目录、执行规则匹配、汇总结果。

扫描过程对样本目录只读，不会新增、修改或删除样本中的任何字段值。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import load_config
from .parsers import ParseFailure, parse_file
from .rules import RISK_ORDER, Rule, builtin_rules, merge_rules
from .security import (
    PathSecurityError,
    SecurityLogger,
    is_supported_file,
    safe_resolve,
    validate_entry_name,
)


@dataclass
class Finding:
    """一条命中记录（同一字段命中多条规则则产生多条记录）。"""

    file: str
    vuln_type: str
    risk: str
    rule: str
    field_path: str
    matched: str
    evidence: str
    advice: str

    def key(self) -> tuple[str, str, str]:
        """基线对比用的稳定标识：文件 + 字段路径 + 规则名。"""
        return self.file, self.field_path, self.rule

    def to_dict(self) -> dict:
        return {
            "file_name": self.file,
            "vuln_type": self.vuln_type,
            "risk_level": self.risk,
            "rule": self.rule,
            "field_path": self.field_path,
            "matched_feature": self.matched,
            "field_value": self.evidence,
            "advice": self.advice,
        }


@dataclass
class ScanResult:
    root: str
    findings: list[Finding] = field(default_factory=list)
    parse_failures: list[dict] = field(default_factory=list)
    rejected_entries: list[dict] = field(default_factory=list)
    config_issues: list[str] = field(default_factory=list)
    parsed_files: list[str] = field(default_factory=list)
    log_path: str = ""

    def sort_findings(self) -> None:
        """风险等级从高到低；同级按文件名升序，再按字段路径/规则保证确定性。"""
        self.findings.sort(
            key=lambda f: (
                RISK_ORDER.get(f.risk, 99),
                f.file.lower(),
                f.field_path,
                f.rule,
            )
        )


class Scanner:
    """对单个授权目录执行完整扫描。"""

    def __init__(
        self,
        input_dir: str | Path,
        security_log: SecurityLogger,
        config_path: str | Path | None = None,
    ):
        self.input_dir = Path(input_dir)
        if not self.input_dir.exists() or not self.input_dir.is_dir():
            raise ValueError(f"输入目录不存在或不是目录: {self.input_dir}")
        self.security_log = security_log
        custom_rules, config_issues = load_config(config_path)
        self.rules = merge_rules(builtin_rules(), custom_rules)
        self._compiled = [(rule, rule.compile()) for rule in self.rules]
        self.result = ScanResult(
            root=str(self.input_dir.resolve()),
            config_issues=config_issues,
            log_path=str(security_log.log_path),
        )

    def scan(self) -> ScanResult:
        # 只列目录第一层条目，确定性排序后逐个处理
        for entry in sorted(self.input_dir.iterdir(), key=lambda p: p.name.lower()):
            self._process_entry(entry.name)
        self.result.sort_findings()
        return self.result

    def _process_entry(self, name: str) -> None:
        """处理单个目录条目（也供测试直接构造非法名称调用）。"""
        ok, reason = validate_entry_name(name)
        if not ok:
            # 与 safe_resolve 走同一拒绝+日志通道
            try:
                safe_resolve(self.input_dir, name, self.security_log)
            except PathSecurityError:
                pass
            self.result.rejected_entries.append(
                {"file_name": name, "reason": reason, "status": "rejected"}
            )
            return

        if not is_supported_file(name):
            # 名称合法但扩展名不允许：不解析、不算安全事件，仅记录跳过
            self.result.rejected_entries.append(
                {"file_name": name, "reason": "扩展名不是 .json/.csv，已跳过",
                 "status": "skipped_unsupported_extension"}
            )
            return

        target = safe_resolve(self.input_dir, name, self.security_log)
        if not target.is_file():
            # 子目录、联接等不作为文件解析
            self.result.rejected_entries.append(
                {"file_name": name, "reason": "不是常规文件，已跳过",
                 "status": "skipped_not_a_file"}
            )
            return

        try:
            field_iter = list(parse_file(target))
        except ParseFailure as exc:
            self.result.parse_failures.append(
                {"file_name": name, "status": "parse_failed", "reason": str(exc)}
            )
            return

        self.result.parsed_files.append(name)
        for field_path, value in field_iter:
            self._match_value(name, field_path, value)

    def _match_value(self, file_name: str, field_path: str, value: str) -> None:
        for rule, compiled in self._compiled:
            match = compiled.search(value)
            if match:
                self.result.findings.append(
                    Finding(
                        file=file_name,
                        vuln_type=rule.vuln_type,
                        risk=rule.risk,
                        rule=rule.name,
                        field_path=field_path,
                        matched=match.group(0),
                        evidence=value,
                        advice=rule.advice,
                    )
                )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
