"""核心引擎：输入加载、规则校验/去重、绕过检测、修复后判定与报告生成。

所有函数均为纯函数或只接收显式参数，保证同一输入多次运行结果可复现。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .matching import fixed_matches, legacy_matches

REQUIRED_FIELDS = ("id", "pattern", "action")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class LoadedSource:
    """一个成功读取并解析的配置来源。"""

    path: str
    rules: list[dict[str, Any]]


@dataclass
class LoadResult:
    sources: list[LoadedSource] = field(default_factory=list)
    # 持续缺失或损坏的输入路径（不得当作空配置静默通过）
    missing_inputs: list[str] = field(default_factory=list)
    # 读取阶段出现过瞬时失败、重试后恢复的路径
    retried_paths: list[str] = field(default_factory=list)


@dataclass
class RuleRecord:
    """通过字段校验并保留下来的规则（首条 id 胜出）。"""

    id: str
    pattern: str
    action: str
    source_path: str


@dataclass
class RuleSet:
    records: list[RuleRecord]                       # 按 id 字典序
    invalid_rules: list[dict[str, str]]             # 字段缺失/类型非法
    error_rules: list[dict[str, str]]               # pattern 无法编译
    duplicate_ids: list[str]                        # 被忽略的重复 id（字典序去重）


@dataclass
class CommandVerdict:
    command: str
    rule_id: str | None
    action: str | None


# --------------------------------------------------------------------------- #
# 输入加载
# --------------------------------------------------------------------------- #
def _read_text_with_retry(
    path: str,
    read_func,
    retry_attempts: int,
) -> tuple[str | None, bool]:
    """读取文本文件。读取失败时按提示重试，返回 (文本, 是否发生过重试)。

    read_func(path) -> str，失败抛出 OSError。
    重试次数耗尽仍失败时返回 (None, retried)。
    """
    attempts = max(1, retry_attempts + 1)
    retried = False
    for attempt in range(1, attempts + 1):
        try:
            return read_func(path), retried
        except OSError:
            if attempt < attempts:
                retried = True
                print(
                    f"[warn] 配置暂时不可读: {path}（第 {attempt} 次失败，"
                    f"可再次尝试，剩余 {attempts - attempt} 次）"
                )
            else:
                print(
                    f"[error] 配置持续不可读: {path}（已重试 {retry_attempts} 次），"
                    f"记入 missing_inputs"
                )
    return None, retried


def load_sources(
    config_paths: list[str],
    retry_attempts: int = 1,
    read_func=None,
) -> LoadResult:
    """依次加载配置路径。

    - 单条路径暂时不可读时提示可再次尝试，重试不影响其它来源继续处理；
    - 持续缺失/不可读，或内容损坏（非法 JSON / 结构错误），计入
      missing_inputs，绝不当作空配置静默通过；
    - 已成功读取的来源完整保留，后续坏输入不会污染它。
    """
    if read_func is None:
        def read_func(p: str) -> str:
            with open(p, "r", encoding="utf-8") as fh:
                return fh.read()

    result = LoadResult()
    for path in config_paths:
        text, retried = _read_text_with_retry(path, read_func, retry_attempts)
        if text is None:
            result.missing_inputs.append(path)
            if retried:
                result.retried_paths.append(path)
            continue
        if retried:
            result.retried_paths.append(path)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            print(f"[error] 配置 JSON 损坏: {path}（{exc.msg}），记入 missing_inputs")
            result.missing_inputs.append(path)
            continue
        if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
            print(
                f"[error] 配置结构非法: {path}（顶层须为对象且含 rules 数组），"
                f"记入 missing_inputs"
            )
            result.missing_inputs.append(path)
            continue
        # 成功解析的数据独立保存，后续来源的任何错误都无法修改它
        result.sources.append(LoadedSource(path=path, rules=data["rules"]))
    return result


def load_commands(path: str, read_func=None) -> tuple[list[str] | None, str | None]:
    """加载命令清单。

    支持两种确定性格式：
    - JSON：{"commands": ["..."]} 或裸 JSON 数组 ["..."]；
    - 纯文本：每行一条命令，空行与 ``#`` 开头的注释行忽略。

    读取/解析失败时返回 (None, 原因)，由调用方记入 missing_inputs。
    """
    if read_func is None:
        def read_func(p: str) -> str:
            with open(p, "r", encoding="utf-8") as fh:
                return fh.read()

    try:
        text = read_func(path)
    except OSError as exc:
        return None, f"命令清单不可读: {exc.__class__.__name__}"

    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, f"命令清单 JSON 损坏: {exc.msg}"
        if isinstance(data, dict):
            data = data.get("commands")
        if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
            return None, "命令清单 JSON 须为字符串数组或含 commands 字符串数组的对象"
        return list(data), None

    commands = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return commands, None


# --------------------------------------------------------------------------- #
# 规则校验、去重与编译
# --------------------------------------------------------------------------- #
def _validate_rule(raw: Any, source_path: str) -> tuple[RuleRecord | None, dict[str, str] | None]:
    """校验单条规则的字段完整性与类型。"""
    if not isinstance(raw, dict):
        return None, {
            "id": "<unknown>",
            "source": source_path,
            "reason": "规则项必须为 JSON 对象",
        }
    missing = [f for f in REQUIRED_FIELDS if f not in raw]
    if missing:
        return None, {
            "id": str(raw.get("id", "<unknown>")),
            "source": source_path,
            "reason": f"缺失字段: {', '.join(missing)}",
        }
    bad_types = [f for f in REQUIRED_FIELDS if not isinstance(raw[f], str)]
    if bad_types:
        return None, {
            "id": str(raw.get("id", "<unknown>")),
            "source": source_path,
            "reason": f"字段类型非法（须为字符串）: {', '.join(bad_types)}",
        }
    if not raw["id"].strip():
        return None, {
            "id": "<empty>",
            "source": source_path,
            "reason": "id 不得为空字符串",
        }
    return (
        RuleRecord(
            id=raw["id"],
            pattern=raw["pattern"],
            action=raw["action"],
            source_path=source_path,
        ),
        None,
    )


def build_rule_set(load_result: LoadResult) -> RuleSet:
    """合并所有成功来源的规则：先校验字段，再按 id 去重（首条胜出）。"""
    invalid_rules: list[dict[str, str]] = []
    error_rules: list[dict[str, str]] = []
    seen: dict[str, RuleRecord] = {}
    duplicate_ids: set[str] = set()

    for source in load_result.sources:
        for raw in source.rules:
            record, invalid = _validate_rule(raw, source.path)
            if invalid is not None:
                invalid_rules.append(invalid)
                continue
            if record.id in seen:
                # 重复 id：保留首条，忽略后续
                duplicate_ids.add(record.id)
                continue
            seen[record.id] = record

    records = sorted(seen.values(), key=lambda r: r.id)

    # pattern 编译失败的规则移入 error_rules，其余规则照常评估
    compilable: list[RuleRecord] = []
    for record in records:
        try:
            re.compile(record.pattern)
        except re.error as exc:
            error_rules.append(
                {
                    "id": record.id,
                    "source": record.source_path,
                    "reason": f"pattern 无法编译: {exc.msg}",
                }
            )
            continue
        compilable.append(record)

    return RuleSet(
        records=compilable,
        invalid_rules=invalid_rules,
        error_rules=error_rules,
        duplicate_ids=sorted(duplicate_ids),
    )


def _compiled_map(rule_set: RuleSet) -> dict[str, re.Pattern[str]]:
    return {r.id: re.compile(r.pattern) for r in rule_set.records}


# --------------------------------------------------------------------------- #
# 绕过检测与修复后判定
# --------------------------------------------------------------------------- #
def detect_vulnerability(
    rule_set: RuleSet,
    commands: list[str],
) -> tuple[list[str], list[str]]:
    """对比历史拼接匹配与修复后语义匹配，返回 (vulnerable_ids, safe_ids)。

    判定标准（确定性）：某条规则在给定命令清单中存在至少一条命令——
    legacy 漏判（未命中）而 fixed 按规则语义命中——则该规则在历史实现下
    可被命令内容绕过，计入 vulnerable_rules；否则计入 safe_rules。
    编译失败/字段非法的规则不参与本评估，不会出现在任一数组中。
    """
    compiled = _compiled_map(rule_set)
    vulnerable: list[str] = []
    safe: list[str] = []
    for record in rule_set.records:
        regex = compiled[record.id]
        bypassed = any(
            (not legacy_matches(record.pattern, cmd)) and fixed_matches(regex, cmd)
            for cmd in commands
        )
        (vulnerable if bypassed else safe).append(record.id)
    return vulnerable, safe  # records 已按 id 排序，结果天然字典序


def evaluate_commands(
    rule_set: RuleSet,
    commands: list[str],
) -> list[CommandVerdict]:
    """修复阶段：按规则自身语义（完整命令上的正则搜索）逐条判定。

    命中多条规则时取 id 字典序最小者，保证结果稳定可复现；
    未命中时 rule_id 与 action 均为 null。
    """
    compiled = _compiled_map(rule_set)
    verdicts: list[CommandVerdict] = []
    for cmd in commands:
        hit: RuleRecord | None = None
        for record in rule_set.records:  # 已按 id 字典序
            if fixed_matches(compiled[record.id], cmd):
                hit = record
                break
        verdicts.append(
            CommandVerdict(
                command=cmd,
                rule_id=hit.id if hit else None,
                action=hit.action if hit else None,
            )
        )
    return verdicts


# --------------------------------------------------------------------------- #
# 报告序列化（键排序 + 固定缩进 + 固定换行，保证逐字节可复现）
# --------------------------------------------------------------------------- #
def build_report(
    rule_set: RuleSet,
    commands: list[str],
    stage: str,
    missing_inputs: list[str],
    retried_paths: list[str],
) -> dict[str, Any]:
    vulnerable, safe = detect_vulnerability(rule_set, commands)
    if stage == "remediated":
        # 修复后同一批命令不再存在可绕过规则：全部可评估规则进入 safe_rules
        vulnerable, safe = [], safe + vulnerable
        vulnerable.sort()
        safe.sort()

    return {
        "stage": stage,
        "summary": {
            "commands_evaluated": len(commands),
            "rules_evaluated": len(rule_set.records),
            "vulnerable_rules": len(vulnerable),
            "safe_rules": len(safe),
            "error_rules": len(rule_set.error_rules),
            "invalid_rules": len(rule_set.invalid_rules),
            "duplicate_ids": len(rule_set.duplicate_ids),
            "missing_inputs": len(missing_inputs),
        },
        "vulnerable_rules": vulnerable,
        "safe_rules": safe,
        "error_rules": rule_set.error_rules,
        "invalid_rules": rule_set.invalid_rules,
        "duplicate_ids": rule_set.duplicate_ids,
        "missing_inputs": list(missing_inputs),
        "retried_paths": list(retried_paths),
    }


def build_judgements(verdicts: list[CommandVerdict], stage: str) -> dict[str, Any]:
    return {
        "stage": stage,
        "results": [
            {"command": v.command, "rule_id": v.rule_id, "action": v.action}
            for v in verdicts
        ],
    }


def dump_json(payload: Any) -> str:
    """确定性 JSON 文本：键排序、无 ASCII 转义、固定缩进与结尾换行。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
