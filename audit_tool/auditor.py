"""离线访问控制复核工具核心模块。

职责
----
1. 按文件名字典序读取输入目录中匹配 ``access_*.json`` 的请求记录；
2. 解析策略文件中“角色 -> 允许路径模式”的映射，模式支持单个星号作末尾通配；
3. 对每条请求依次做：畸形校验、角色补全（缺省/null -> 匿名）、方法过滤、
   点段规范化、静态资源过滤、授权判定；
4. 将畸形记录写入 ``malformed.log``（含来源文件、记录序号、原因）；
5. 生成 ``report.json``，含越权列表、冲突列表与汇总。

设计约定（保证审计结论可复核）
------------------------------
* 只使用 Python 标准库，不访问网络；
* 路径模式中星号仅允许出现在末尾（如 ``/admin/*``），其余位置视为策略错误；
* 同角色同模式同时存在允许与拒绝策略 => 策略冲突，单列且不计入越权；
* 路径未命中任何策略模式 => 无适用策略，不计越权（审计上单独列示）；
* 静态资源不做越权判定，但计入已处理总数。
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

ANONYMOUS_ROLE = "anonymous"
ALLOWED_METHODS = ("GET", "POST")
# 静态资源后缀（点号小写），命中即不做授权判定，但仍计入已处理总数。
STATIC_EXTENSIONS = frozenset(
    {".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2"}
)
INPUT_FILE_GLOB = "access_*.json"
REPORT_NAME = "report.json"
MALFORMED_LOG_NAME = "malformed.log"


class PolicyError(Exception):
    """策略文件缺失、不可读或内容非法。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PolicyRule:
    """一条角色策略。effect 取值 allow / deny。"""

    role: str
    pattern: str
    effect: str


@dataclass
class Policy:
    """解析后的角色路径策略。"""

    rules: list[PolicyRule]
    #: (role, pattern) 同时出现 allow 与 deny 的策略冲突
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    #: 角色 -> 允许模式列表（按策略文件出现顺序）
    role_allows: dict[str, list[str]] = field(default_factory=dict)
    #: 角色 -> 拒绝模式列表
    role_denies: dict[str, list[str]] = field(default_factory=dict)

    def conflicted_patterns_for(self, role: str) -> set[str]:
        return {
            item["pattern"]
            for item in self.conflicts
            if item["role"] == role
        }


# --------------------------------------------------------------------------- #
# 策略加载
# --------------------------------------------------------------------------- #
def _validate_pattern(pattern: Any) -> str:
    if not isinstance(pattern, str) or not pattern:
        raise PolicyError(f"策略模式必须是非空字符串，实际为: {pattern!r}")
    if not pattern.startswith("/"):
        raise PolicyError(f"策略模式必须以 / 开头: {pattern!r}")
    star = pattern.find("*")
    if star != -1 and star != len(pattern) - 1:
        raise PolicyError(f"星号仅允许作为末尾通配: {pattern!r}")
    return pattern


def load_policy(path: str | os.PathLike[str]) -> Policy:
    """加载并校验策略文件。

    支持的 JSON 结构::

        {
          "roles": {"admin": ["/admin/*", "/"], "user": ["/user/*"]},
          "denials": [{"role": "user", "pattern": "/admin/*"}]
        }

    ``denials`` 可选，用于构造“同一角色同一模式既允许又拒绝”的冲突场景，
    以及命中即拒绝的拒绝型策略。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError as exc:
        raise PolicyError(f"策略文件不存在: {path}") from exc
    except PermissionError as exc:
        raise PolicyError(f"策略文件不可读（权限不足）: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PolicyError(f"策略文件不是合法 JSON: {path}（{exc}）") from exc
    except OSError as exc:
        raise PolicyError(f"策略文件读取失败: {path}（{exc}）") from exc

    if not isinstance(raw, dict):
        raise PolicyError("策略文件顶层必须是 JSON 对象")
    roles = raw.get("roles")
    if not isinstance(roles, dict):
        raise PolicyError("策略文件缺少 roles 映射（角色 -> 模式数组）")

    rules: list[PolicyRule] = []
    role_allows: dict[str, list[str]] = {}
    role_denies: dict[str, list[str]] = {}

    for role, patterns in roles.items():
        if not isinstance(role, str) or not role:
            raise PolicyError(f"角色名必须是非空字符串，实际为: {role!r}")
        if not isinstance(patterns, list):
            raise PolicyError(f"角色 {role!r} 的模式必须是数组")
        for pattern in patterns:
            pattern = _validate_pattern(pattern)
            rules.append(PolicyRule(role, pattern, "allow"))
            role_allows.setdefault(role, []).append(pattern)

    denials = raw.get("denials", [])
    if denials is None:
        denials = []
    if not isinstance(denials, list):
        raise PolicyError("denials 必须是数组")
    for item in denials:
        if not isinstance(item, dict):
            raise PolicyError(f"denials 元素必须是对象，实际为: {item!r}")
        role = item.get("role")
        if not isinstance(role, str) or not role:
            raise PolicyError(f"denials 中角色名非法: {item!r}")
        pattern = _validate_pattern(item.get("pattern"))
        rules.append(PolicyRule(role, pattern, "deny"))
        role_denies.setdefault(role, []).append(pattern)

    # 冲突检测：同角色同模式同时存在 allow 与 deny。
    conflicts: list[dict[str, Any]] = []
    for role, deny_patterns in role_denies.items():
        allow_set = set(role_allows.get(role, ()))
        for pattern in deny_patterns:
            if pattern in allow_set:
                conflicts.append(
                    {
                        "role": role,
                        "pattern": pattern,
                        "reason": "同一角色对该模式同时存在 allow 与 deny 策略",
                    }
                )
    conflicts.sort(key=lambda c: (c["role"], c["pattern"]))

    return Policy(
        rules=rules,
        conflicts=conflicts,
        role_allows=role_allows,
        role_denies=role_denies,
    )


# --------------------------------------------------------------------------- #
# 路径处理与模式匹配
# --------------------------------------------------------------------------- #
def normalize_path(raw_path: str) -> str:
    """点段规范化：先剥离查询串/片段，再用 POSIX 规则解析 ``.`` 与 ``..``。

    连续的 ``//`` 折叠为单个 ``/``；保留末尾斜杠；结果始终以 ``/`` 开头。
    例如 ``/admin/../user/x`` -> ``/user/x``。
    """
    # 只取路径部分，去掉 ?query 与 #fragment。
    path = raw_path.split("#", 1)[0].split("?", 1)[0]
    # 预折叠重复斜杠，避免 normpath 的 // 特殊语义。
    while "//" in path:
        path = path.replace("//", "/")
    trailing = path.endswith("/") and len(path) > 1
    normalized = posixpath.normpath(path)
    if trailing and not normalized.endswith("/"):
        normalized += "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def pattern_matches(pattern: str, path: str) -> bool:
    """判定路径是否命中模式。

    * 无星号：完全相等才算命中；
    * 末尾 ``/*``：命中该目录前缀下的路径（``/admin/*`` 命中
      ``/admin/settings``，不命中 ``/user/x``，也不命中 ``/admin`` 本身）。
    """
    if not pattern.endswith("*"):
        return pattern == path
    prefix = pattern[:-1]  # 例如 /admin/
    return path.startswith(prefix) and len(path) > len(prefix)


def is_static_resource(path: str) -> bool:
    """按路径最后一段的后缀识别静态资源。"""
    base = path.rsplit("/", 1)[-1]
    _, ext = os.path.splitext(base)
    return ext.lower() in STATIC_EXTENSIONS


# --------------------------------------------------------------------------- #
# 单条记录校验与判定
# --------------------------------------------------------------------------- #
@dataclass
class PreparedRecord:
    """通过畸形校验后的请求记录。"""

    method: str
    path: str
    role: str
    raw_index: int


def validate_record(
    record: Any, index: int
) -> tuple[PreparedRecord | None, str | None]:
    """校验单条记录。

    返回 ``(PreparedRecord, None)`` 或 ``(None, 畸形原因)``。
    注意：缺 method、角色缺失属于业务规则处理范围，不算畸形。
    """
    if not isinstance(record, dict):
        return None, "记录不是 JSON 对象"

    raw_path = record.get("path")
    if raw_path is None:
        return None, "缺少 path 字段"
    if not isinstance(raw_path, str):
        return None, f"path 不是字符串: {raw_path!r}"
    if not raw_path.strip():
        return None, "path 为空字符串"
    if not raw_path.lstrip().startswith("/"):
        return None, f"path 必须以 / 开头: {raw_path!r}"

    method = record.get("method")
    if method is None:
        method_norm = ""  # 缺 method：不参与授权判定，但记录本身不畸形。
    elif not isinstance(method, str):
        return None, f"method 不是字符串: {method!r}"
    else:
        method_norm = method.strip().upper()
        if not method_norm:
            method_norm = ""

    role = record.get("role")
    if role is None or (isinstance(role, str) and not role.strip()):
        role_norm = ANONYMOUS_ROLE
    elif not isinstance(role, str):
        return None, f"role 不是字符串或 null: {role!r}"
    else:
        role_norm = role.strip()

    return (
        PreparedRecord(
            method=method_norm,
            path=normalize_path(raw_path),
            role=role_norm,
            raw_index=index,
        ),
        None,
    )


def evaluate(rec: PreparedRecord, policy: Policy) -> dict[str, Any]:
    """对一条已规范化的请求做授权判定，返回审计条目。"""

    matched = [
        rule
        for rule in policy.rules
        if rule.role == rec.role and pattern_matches(rule.pattern, rec.path)
    ]
    matched_patterns = sorted({rule.pattern for rule in matched})
    # 需求第 7 条的“任一策略模式”：跨所有角色的全局命中集合。
    global_patterns = sorted(
        {rule.pattern for rule in policy.rules if pattern_matches(rule.pattern, rec.path)}
    )

    base = {
        "source_index": rec.raw_index,
        "method": rec.method,
        "path": rec.path,
        "role": rec.role,
        "matched_patterns": matched_patterns,
        "policy_matched_patterns": global_patterns,
    }

    # 1) 策略冲突优先：同角色同模式 allow/deny 同时命中，单列、不计越权。
    conflicted = policy.conflicted_patterns_for(rec.role)
    hit_conflicts = sorted(p for p in matched_patterns if p in conflicted)
    if hit_conflicts:
        return {
            **base,
            "category": "policy_conflict",
            "conflict_patterns": hit_conflicts,
            "reason": "命中的模式存在互相冲突的角色策略，授权结论不可判定",
        }

    effects = {rule.effect for rule in matched}
    if "allow" in effects:
        return {**base, "category": "allowed", "decision": "allow"}
    if "deny" in effects:
        return {
            **base,
            "category": "unauthorized",
            "hit_pattern": matched_patterns[0],
            "reason": "角色对命中的模式仅存在拒绝策略",
        }

    if global_patterns:
        # 角色自身无允许规则，但路径命中了策略中的任一模式 => 越权。
        return {
            **base,
            "category": "unauthorized",
            "hit_pattern": global_patterns[0],
            "reason": "角色无权访问该路径且路径命中策略模式",
        }
    return {
        **base,
        "category": "no_policy_match",
        "reason": "路径未命中任何策略模式，无适用策略（不计越权）",
    }


# --------------------------------------------------------------------------- #
# 主审计流程
# --------------------------------------------------------------------------- #
def _read_input_file(
    file_path: str,
) -> tuple[list[Any] | None, str | None]:
    """读取一个输入文件，返回 (记录数组, 来源级错误原因)。"""
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None, "文件不存在"
    except PermissionError:
        return None, "文件不可读（权限不足）"
    except json.JSONDecodeError as exc:
        return None, f"文件不是合法 JSON: {exc}"
    except OSError as exc:
        return None, f"文件读取失败: {exc}"
    if not isinstance(data, list):
        return None, "文件顶层必须是记录数组"
    return data, None


def run_audit(input_dir: str | os.PathLike[str], policy: Policy) -> dict[str, Any]:
    """执行完整审计，返回 report.json 对应的报告对象。"""

    if not os.path.isdir(input_dir):
        raise FileNotFoundError(f"输入目录不存在或不是目录: {input_dir}")
    if not os.access(input_dir, os.R_OK):
        raise PermissionError(f"输入目录不可读（权限不足）: {input_dir}")

    names = sorted(
        name
        for name in os.listdir(input_dir)
        if name.startswith("access_") and name.endswith(".json")
        and os.path.isfile(os.path.join(input_dir, name))
    )

    unauthorized: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    source_failures: list[dict[str, Any]] = []
    ignored_method: list[dict[str, Any]] = []
    static_skipped: list[dict[str, Any]] = []
    allowed: list[dict[str, Any]] = []
    no_policy: list[dict[str, Any]] = []
    roles_involved: set[str] = set()

    def _entry(rec: PreparedRecord, source: str) -> dict[str, Any]:
        return {
            "source_file": source,
            "source_index": rec.raw_index,
            "method": rec.method or None,
            "path": rec.path,
            "role": rec.role,
        }

    total_records = 0
    for name in names:
        file_path = os.path.join(input_dir, name)
        records, source_error = _read_input_file(file_path)
        if source_error is not None:
            # 整体不可用来源：与逐条畸形输入分开列示。
            source_failures.append({"source_file": name, "reason": source_error})
            continue

        for index, record in enumerate(records, start=1):
            total_records += 1
            prepared, reason = validate_record(record, index)
            if prepared is None:
                malformed.append(
                    {"source_file": name, "source_index": index, "reason": reason}
                )
                continue

            roles_involved.add(prepared.role)

            if prepared.method not in ALLOWED_METHODS:
                # PUT/DELETE/缺 method 等：不参与授权判定。
                ignored_method.append(
                    {
                        **_entry(prepared, name),
                        "reason": (
                            "缺少 method 字段"
                            if not prepared.method
                            else f"不支持的方法 {prepared.method}（仅接受 GET/POST）"
                        ),
                    }
                )
                continue

            if is_static_resource(prepared.path):
                static_skipped.append(
                    {
                        **_entry(prepared, name),
                        "reason": "静态资源访问不纳入越权判定",
                    }
                )
                continue

            verdict = evaluate(prepared, policy)
            verdict["source_file"] = name
            category = verdict.pop("category")
            if category == "unauthorized":
                unauthorized.append(verdict)
            elif category == "policy_conflict":
                conflicts.append(verdict)
            elif category == "allowed":
                allowed.append(verdict)
            else:
                no_policy.append(verdict)

    summary = {
        "input_files_found": len(names),
        "input_files": names,
        "processed_total": total_records,
        "unauthorized_count": len(unauthorized),
        "conflict_count": len(conflicts),
        "malformed_count": len(malformed),
        "source_failure_count": len(source_failures),
        "ignored_method_count": len(ignored_method),
        "static_skipped_count": len(static_skipped),
        "allowed_count": len(allowed),
        "no_policy_match_count": len(no_policy),
        "roles_involved": sorted(roles_involved),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    # 可复核的恒等式：总数 == 各分流之和。
    summary["count_cross_check"] = (
        len(unauthorized)
        + len(conflicts)
        + len(malformed)
        + len(ignored_method)
        + len(static_skipped)
        + len(allowed)
        + len(no_policy)
    )

    report: dict[str, Any] = {
        "report_version": 1,
        "summary": summary,
        "unauthorized_requests": unauthorized,
        "policy_conflicts": conflicts,
        "policy_definition_conflicts": policy.conflicts,
        "malformed_records": malformed,
        "unavailable_sources": source_failures,
        "ignored_by_method": ignored_method,
        "static_resources_skipped": static_skipped,
        "allowed_requests": allowed,
        "no_policy_match_requests": no_policy,
    }

    if not names:
        report["notice"] = (
            "输入目录中没有匹配 access_*.json 的文件；"
            "本报告基于空输入生成，所有计数为 0。"
        )
        summary["notice"] = "no_matching_input_files"
    if source_failures:
        report["notice"] = (
            "存在整体不可用的输入来源，详见 unavailable_sources；"
            "这些来源未参与逐条判定。"
        )

    return report


def write_outputs(report: dict[str, Any], output_dir: str | os.PathLike[str]) -> None:
    """原子写入 report.json 与 malformed.log，避免产生残缺报告。"""
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, REPORT_NAME)
    log_path = os.path.join(output_dir, MALFORMED_LOG_NAME)

    tmp_report = report_path + ".tmp"
    with open(tmp_report, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp_report, report_path)

    lines = []
    for item in report["malformed_records"]:
        lines.append(
            f"{item['source_file']} #{item['source_index']} 畸形记录，原因: {item['reason']}"
        )
    for item in report["unavailable_sources"]:
        lines.append(
            f"{item['source_file']} 来源整体不可用，原因: {item['reason']}"
        )
    with open(log_path, "w", encoding="utf-8") as fh:
        if lines:
            fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _force_utf8_console() -> None:
    """Windows 控制台默认 GBK 代码页会让中文输出乱码，尽力切换为 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="access_auditor",
        description="离线访问控制复核：基于角色路径策略审计请求记录，识别越权与策略冲突。",
    )
    parser.add_argument(
        "--input-dir",
        default=".",
        help="请求记录目录，仅处理其中匹配 access_*.json 的文件（默认: 当前目录）",
    )
    parser.add_argument(
        "--policy",
        default="policy.json",
        help="角色路径策略文件路径（默认: policy.json）",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="report.json 与 malformed.log 的输出目录（默认: 当前目录）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    args = build_parser().parse_args(argv)

    # 前置检查：任何一项不过都不产出残缺报告。
    if not os.path.exists(args.policy):
        print(f"错误: 策略文件不存在: {args.policy}", file=sys.stderr)
        return 1
    if not os.path.isfile(args.policy):
        print(f"错误: 策略路径不是文件: {args.policy}", file=sys.stderr)
        return 1
    if not os.path.exists(args.input_dir):
        print(f"错误: 输入目录不存在: {args.input_dir}", file=sys.stderr)
        return 1
    if not os.path.isdir(args.input_dir):
        print(f"错误: 输入路径不是目录: {args.input_dir}", file=sys.stderr)
        return 1
    if not os.access(args.input_dir, os.R_OK):
        print(f"错误: 输入目录不可读（权限不足）: {args.input_dir}", file=sys.stderr)
        return 1

    try:
        policy = load_policy(args.policy)
    except PolicyError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    try:
        report = run_audit(args.input_dir, policy)
        write_outputs(report, args.output_dir)
    except (OSError, PermissionError, FileNotFoundError) as exc:
        print(f"错误: 审计执行失败: {exc}", file=sys.stderr)
        return 1

    s = report["summary"]
    print("离线访问控制复核完成")
    print(f"  输入目录          : {args.input_dir}")
    print(f"  匹配输入文件数    : {s['input_files_found']} -> {', '.join(s['input_files']) or '（无）'}")
    print(f"  已处理请求总数    : {s['processed_total']}")
    print(f"  越权条目          : {s['unauthorized_count']}")
    print(f"  策略冲突条目      : {s['conflict_count']}")
    print(f"  畸形记录          : {s['malformed_count']}（见 {MALFORMED_LOG_NAME}）")
    print(f"  来源不可用        : {s['source_failure_count']}")
    print(f"  方法过滤          : {s['ignored_method_count']}")
    print(f"  静态资源过滤      : {s['static_skipped_count']}")
    print(f"  允许/无策略匹配   : {s['allowed_count']} / {s['no_policy_match_count']}")
    print(f"  涉及角色（字典序）: {', '.join(s['roles_involved']) or '（无）'}")
    print(f"  计数交叉校验      : {s['processed_total']} == {s['count_cross_check']}")
    if "notice" in report:
        print(f"  备注              : {report['notice']}")
    print(f"  报告已写入        : {os.path.join(args.output_dir, REPORT_NAME)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
