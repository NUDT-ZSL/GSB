"""用例 JSON 的加载与结构校验。

校验失败时抛出 CaseValidationError，errors 中给出逐条字段级错误（JSON Pointer 风格路径）。
该校验发生在任何网络请求之前，CLI 收到异常后直接终止，不发起请求。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CASE_TYPES = {"sqli", "idor", "business"}
_ATTACK_INTENTS = {"attack", "negative_control"}
_METHODS = {"GET", "POST"}
_PARAM_RULES = {"not_empty", "numeric", "positive_int"}
_RULE_TYPES = {
    "status_code_equals",
    "json_path_equals",
    "json_len_gte",
    "json_len_equals",
    "body_contains",
    "body_not_contains",
}


class CaseValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _is_int(value: Any) -> bool:
    # bool 是 int 的子类，显式排除
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def _validate_rules(rules: Any, path: str, errors: list[str]) -> None:
    if not isinstance(rules, list) or not rules:
        errors.append(f"{path}: 必须为非空数组，每条用例至少配置一条命中判定规则")
        return
    for i, rule in enumerate(rules):
        rpath = f"{path}[{i}]"
        if not isinstance(rule, dict):
            errors.append(f"{rpath}: 规则必须为对象，实际类型 {_type_name(rule)}")
            continue
        rtype = rule.get("type")
        if not _is_str(rtype) or not rtype:
            errors.append(f"{rpath}.type: 必填字段，须为非空字符串")
        elif rtype not in _RULE_TYPES:
            errors.append(
                f"{rpath}.type: 未知规则类型 {rtype!r}，允许值 {sorted(_RULE_TYPES)}"
            )
        if "expected" not in rule:
            errors.append(f"{rpath}.expected: 必填字段（命中期望值）")
        else:
            expected = rule["expected"]
            if rtype in ("status_code_equals", "json_len_gte", "json_len_equals"):
                if not _is_int(expected):
                    errors.append(
                        f"{rpath}.expected: 规则 {rtype} 要求整数，"
                        f"实际类型 {_type_name(expected)}"
                    )
            elif rtype in ("json_path_equals", "body_contains", "body_not_contains"):
                if not isinstance(expected, (str, int, float, bool)):
                    errors.append(
                        f"{rpath}.expected: 规则 {rtype} 要求标量值，"
                        f"实际类型 {_type_name(expected)}"
                    )
        if rtype and rtype.startswith("json_"):
            jp = rule.get("path")
            if not _is_str(jp) or not jp:
                errors.append(f"{rpath}.path: JSON 类规则必须提供非空字符串 path")


def _validate_param_rules(param_rules: Any, path: str, errors: list[str]) -> None:
    if not isinstance(param_rules, dict) or not param_rules:
        errors.append(f"{path}: 必须为非空对象（参数名 -> 校验规则数组）")
        return
    for name, rule_list in param_rules.items():
        ppath = f"{path}.{name}"
        if not _is_str(name) or not name:
            errors.append(f"{path}: 参数名必须为非空字符串")
        if not isinstance(rule_list, list) or not rule_list:
            errors.append(f"{ppath}: 必须为非空数组")
            continue
        for j, item in enumerate(rule_list):
            if item not in _PARAM_RULES:
                errors.append(
                    f"{ppath}[{j}]: 未知参数规则 {item!r}，允许值 {sorted(_PARAM_RULES)}"
                )


def _validate_case(case: Any, index: int, errors: list[str]) -> None:
    cpath = f"$.cases[{index}]"
    if not isinstance(case, dict):
        errors.append(f"{cpath}: 用例必须为对象，实际类型 {_type_name(case)}")
        return
    for field in ("case_id", "type", "description", "request"):
        if field not in case:
            errors.append(f"{cpath}.{field}: 必填字段缺失")

    case_id = case.get("case_id")
    if "case_id" in case and (not _is_str(case_id) or not case_id):
        errors.append(f"{cpath}.case_id: 必须为非空字符串")

    ctype = case.get("type")
    if "type" in case and (not _is_str(ctype) or ctype not in _CASE_TYPES):
        errors.append(
            f"{cpath}.type: 必须为 {sorted(_CASE_TYPES)} 之一，实际值 {ctype!r}"
        )

    if "attack_intent" in case and (
        not _is_str(case.get("attack_intent"))
        or case["attack_intent"] not in _ATTACK_INTENTS
    ):
        errors.append(
            f"{cpath}.attack_intent: 若提供必须为 {sorted(_ATTACK_INTENTS)} 之一"
        )

    if "description" in case and not _is_str(case.get("description")):
        errors.append(f"{cpath}.description: 必须为字符串")

    request = case.get("request")
    if "request" in case:
        if not isinstance(request, dict):
            errors.append(f"{cpath}.request: 必须为对象，实际类型 {_type_name(request)}")
        else:
            rpath = f"{cpath}.request"
            for field in ("method", "path", "headers", "params"):
                if field not in request:
                    errors.append(f"{rpath}.{field}: 必填字段缺失")
            method = request.get("method")
            if "method" in request and (
                not _is_str(method) or method.upper() not in _METHODS
            ):
                errors.append(
                    f"{rpath}.method: 必须为 {sorted(_METHODS)} 之一，实际值 {method!r}"
                )
            if "path" in request and (not _is_str(request.get("path")) or not request.get("path")):
                errors.append(f"{rpath}.path: 必须为非空字符串")
            if "headers" in request and not isinstance(request.get("headers"), dict):
                errors.append(
                    f"{rpath}.headers: 必须为对象，实际类型 {_type_name(request.get('headers'))}"
                )
            else:
                for hname, hvalue in request.get("headers", {}).items():
                    if not _is_str(hname) or not hname:
                        errors.append(f"{rpath}.headers: 头部名必须为非空字符串")
                    if not _is_str(hvalue):
                        errors.append(
                            f"{rpath}.headers.{hname}: 头部值必须为字符串，"
                            f"实际类型 {_type_name(hvalue)}"
                        )
            if "params" in request and not isinstance(request.get("params"), dict):
                errors.append(
                    f"{rpath}.params: 必须为对象，实际类型 {_type_name(request.get('params'))}"
                )
            else:
                for pname, pvalue in request.get("params", {}).items():
                    if not _is_str(pname) or not pname:
                        errors.append(f"{rpath}.params: 参数名必须为非空字符串")
                    if not isinstance(pvalue, (str, int, float, bool)):
                        errors.append(
                            f"{rpath}.params.{pname}: 参数值必须为标量，"
                            f"实际类型 {_type_name(pvalue)}"
                        )
            if "body" in request and not isinstance(request.get("body"), dict):
                errors.append(f"{rpath}.body: 若提供必须为对象")

    if "rules" in case:
        _validate_rules(case["rules"], f"{cpath}.rules", errors)
    elif isinstance(case, dict):
        errors.append(f"{cpath}.rules: 必填字段缺失（命中判定规则）")

    if "param_rules" in case:
        _validate_param_rules(case["param_rules"], f"{cpath}.param_rules", errors)

    if "base_url" in case and not _is_str(case.get("base_url")):
        errors.append(f"{cpath}.base_url: 若提供必须为字符串")

    if "compare_ignore_fields" in case and not (
        isinstance(case.get("compare_ignore_fields"), list)
        and all(_is_str(x) for x in case["compare_ignore_fields"])
    ):
        errors.append(f"{cpath}.compare_ignore_fields: 若提供必须为字符串数组")


def load_case_file(path: str | Path) -> dict[str, Any]:
    """读取并完整校验用例文件，返回规范化的数据对象。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise CaseValidationError([f"$.file: 用例文件不存在：{file_path}"])
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CaseValidationError([f"$.file: 无法读取用例文件：{exc}"]) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CaseValidationError(
            [f"$.json: 用例文件不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）"]
        ) from exc

    errors: list[str] = []
    if not isinstance(data, dict):
        raise CaseValidationError([f"$: 根节点必须为对象，实际类型 {_type_name(data)}"])

    for field in ("target", "cases"):
        if field not in data:
            errors.append(f"$.{field}: 必填字段缺失")

    target = data.get("target")
    if "target" in data:
        if not isinstance(target, dict):
            errors.append(f"$.target: 必须为对象，实际类型 {_type_name(target)}")
        else:
            if not _is_str(target.get("base_url")) or not target.get("base_url"):
                errors.append("$.target.base_url: 必填字段，须为非空字符串")
            if "description" in target and not _is_str(target.get("description")):
                errors.append("$.target.description: 必须为字符串")

    cases = data.get("cases")
    if "cases" in data:
        if not isinstance(cases, list) or not cases:
            errors.append("$.cases: 必须为非空数组")
        else:
            seen_ids: set[str] = set()
            for i, case in enumerate(cases):
                _validate_case(case, i, errors)
                if isinstance(case, dict) and _is_str(case.get("case_id")):
                    if case["case_id"] in seen_ids:
                        errors.append(
                            f"$.cases[{i}].case_id: 用例编号 {case['case_id']!r} 重复"
                        )
                    seen_ids.add(case["case_id"])

    if errors:
        raise CaseValidationError(errors)
    return data
