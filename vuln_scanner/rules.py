"""危险模式与注入检测规则。

内置规则覆盖 SQL 注入与命令注入特征；本地自定义配置中的规则会与
内置规则合并使用（同名规则以自定义配置为准）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

RISK_HIGH = "high"
RISK_MEDIUM = "medium"
RISK_LOW = "low"

RISK_ORDER = {RISK_HIGH: 0, RISK_MEDIUM: 1, RISK_LOW: 2}

# 漏洞类型常量
TYPE_SQL_INJECTION = "SQL注入"
TYPE_CMD_INJECTION = "命令注入"
TYPE_XSS = "跨站脚本特征"

_ADVICE_SQL = "使用参数化查询/预编译语句，禁止把字段值直接拼接进 SQL；对输入做白名单校验。"
_ADVICE_CMD = "禁止把用户输入拼接进 shell 命令；如必须调用外部程序，使用参数数组形式并做白名单校验。"
_ADVICE_XSS = "输出到页面前按上下文进行 HTML 转义；富文本场景使用成熟净化库。"


@dataclass(frozen=True)
class Rule:
    """单条检测规则。

    pattern 为正则表达式；literal=True 时按大小写不敏感的字面量子串匹配。
    """

    name: str
    vuln_type: str
    risk: str
    pattern: str
    advice: str
    literal: bool = False

    def compile(self) -> re.Pattern[str]:
        if self.literal:
            return re.compile(re.escape(self.pattern), re.IGNORECASE)
        return re.compile(self.pattern, re.IGNORECASE)


# 内置确定性规则集
BUILTIN_RULES: tuple[Rule, ...] = (
    Rule("sql_drop_table", TYPE_SQL_INJECTION, RISK_HIGH,
         r"\bDROP\s+TABLE\b", _ADVICE_SQL),
    Rule("sql_union_select", TYPE_SQL_INJECTION, RISK_HIGH,
         r"\bUNION\s+(ALL\s+)?SELECT\b", _ADVICE_SQL),
    Rule("sql_or_tautology", TYPE_SQL_INJECTION, RISK_HIGH,
         r"(?:'\s*OR\s*'?\d*'?\s*=\s*'?\d*|--\s*$|/\*.*?\*/)", _ADVICE_SQL),
    Rule("sql_insert_into", TYPE_SQL_INJECTION, RISK_MEDIUM,
         r"\bINSERT\s+INTO\b", _ADVICE_SQL),
    Rule("cmd_shell_sep", TYPE_CMD_INJECTION, RISK_HIGH,
         r";\s*(?:rm|cat|ls|ping|wget|curl|powershell|cmd|bash|sh)\b", _ADVICE_CMD),
    Rule("cmd_substitution", TYPE_CMD_INJECTION, RISK_HIGH,
         r"\$\([^)]+\)|`[^`]+`", _ADVICE_CMD),
    Rule("cmd_pipe_shell", TYPE_CMD_INJECTION, RISK_MEDIUM,
         r"\|\s*(?:sh|bash|cmd|powershell)\b", _ADVICE_CMD),
    Rule("cmd_chain_separator", TYPE_CMD_INJECTION, RISK_LOW,
         r"&&\s*\w+", _ADVICE_CMD),
)


def builtin_rules() -> list[Rule]:
    return list(BUILTIN_RULES)


def load_custom_rules(config: dict) -> tuple[list[Rule], list[str]]:
    """从已解析的配置 dict 中提取自定义规则。

    返回 ``(规则列表, 配置问题描述列表)``。单条规则格式错误只跳过该条，
    不影响其余规则。
    """
    issues: list[str] = []
    rules: list[Rule] = []
    raw_rules = config.get("patterns")
    if not isinstance(raw_rules, list):
        issues.append("配置项 patterns 缺失或不是数组，自定义规则未加载")
        return rules, issues

    valid_risk = {RISK_HIGH, RISK_MEDIUM, RISK_LOW}
    type_map = {
        "sql_injection": TYPE_SQL_INJECTION,
        "command_injection": TYPE_CMD_INJECTION,
        "xss": TYPE_XSS,
    }
    advice_map = {
        TYPE_SQL_INJECTION: _ADVICE_SQL,
        TYPE_CMD_INJECTION: _ADVICE_CMD,
        TYPE_XSS: _ADVICE_XSS,
    }

    for idx, item in enumerate(raw_rules):
        where = f"patterns[{idx}]"
        if not isinstance(item, dict):
            issues.append(f"{where} 不是对象，已跳过")
            continue
        name = item.get("name")
        pattern = item.get("pattern")
        if not isinstance(name, str) or not name.strip():
            issues.append(f"{where} 缺少合法 name，已跳过")
            continue
        if not isinstance(pattern, str) or not pattern.strip():
            issues.append(f"{where}({name}) 缺少合法 pattern，已跳过")
            continue
        vuln_type = type_map.get(str(item.get("type", "")).lower())
        if vuln_type is None:
            issues.append(f"{where}({name}) type 非法，已跳过")
            continue
        risk = str(item.get("risk", "medium")).lower()
        if risk not in valid_risk:
            issues.append(f"{where}({name}) risk 非法，已跳过")
            continue
        literal = bool(item.get("literal", False))
        if not literal:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                issues.append(f"{where}({name}) 正则表达式无效: {exc}，已跳过")
                continue
        rules.append(Rule(
            name=name.strip(),
            vuln_type=vuln_type,
            risk=risk,
            pattern=pattern,
            advice=str(item.get("advice") or advice_map[vuln_type]),
            literal=literal,
        ))
    return rules, issues


def merge_rules(base: list[Rule], custom: list[Rule]) -> list[Rule]:
    """合并内置与自定义规则；同名规则以自定义配置覆盖。"""
    merged = {rule.name: rule for rule in base}
    for rule in custom:
        merged[rule.name] = rule
    # 名称排序保证规则顺序确定
    return [merged[name] for name in sorted(merged)]
