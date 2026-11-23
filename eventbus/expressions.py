"""Deterministic filter and transform DSL."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

from .errors import err
from .util import (
    canonical_json,
    deterministic_number,
    ensure_json_value,
    is_int,
    to_decimal,
)

_FILTER_OPS = {
    "eq",
    "neq",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "contains",
    "exists",
}
_TRANSFORM_OPS = {
    "set",
    "rename",
    "copy",
    "delete",
    "default",
    "add",
    "multiply",
    "round",
}


def validate_filter(document: dict[str, Any], path: str = "filter") -> None:
    if not isinstance(document, dict):
        raise err("PATCH_INVALID", "过滤条件必须为对象", path=path)
    if not document:
        return
    logical = [key for key in ("all", "any", "not") if key in document]
    if logical:
        if len(document) != 1:
            raise err("PATCH_INVALID", "逻辑过滤对象只能包含一个逻辑键", path=path)
        key = logical[0]
        value = document[key]
        if key == "not":
            validate_filter(value, f"{path}.not")
            return
        if not isinstance(value, list) or not value:
            raise err("PATCH_INVALID", "all/any 必须为非空条件数组", path=path)
        for index, item in enumerate(value):
            validate_filter(item, f"{path}.{key}[{index}]")
        return
    field = document.get("field")
    op = document.get("op")
    if not isinstance(field, str) or not field:
        raise err("PATCH_INVALID", "过滤条件 field 必须为非空字符串", path=path)
    if op not in _FILTER_OPS:
        raise err("PATCH_INVALID", "过滤操作符不合法", path=path, op=op)
    if op == "exists":
        if not isinstance(document.get("value"), bool):
            raise err("PATCH_INVALID", "exists 的 value 必须为布尔值", path=path)
        return
    if "value" not in document:
        raise err("PATCH_INVALID", "过滤条件缺少 value", path=path)
    ensure_json_value(document["value"], path=f"{path}.value")
    if op == "in" and not isinstance(document["value"], list):
        raise err("PATCH_INVALID", "in 的 value 必须为数组", path=path)
    if op in {"gt", "gte", "lt", "lte"}:
        to_decimal(document["value"])


def validate_transforms(transforms: list[dict[str, Any]], path: str = "transforms") -> None:
    if not isinstance(transforms, list):
        raise err("PATCH_INVALID", "变换表达式必须为数组", path=path)
    for index, item in enumerate(transforms):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            raise err("PATCH_INVALID", "变换步骤必须为对象", path=item_path)
        op = item.get("op")
        if op not in _TRANSFORM_OPS:
            raise err("PATCH_INVALID", "变换操作符不合法", path=item_path, op=op)
        if op in {"set", "default"}:
            _require_field(item, item_path)
            if "value" not in item:
                raise err("PATCH_INVALID", "变换步骤缺少 value", path=item_path)
            ensure_json_value(item["value"], path=f"{item_path}.value")
        elif op in {"rename", "copy"}:
            _require_field(item, item_path, "from")
            _require_field(item, item_path, "to")
        elif op == "delete":
            _require_field(item, item_path)
        elif op in {"add", "multiply"}:
            _require_field(item, item_path)
            to_decimal(item.get("value"))
            scale = item.get("scale")
            if scale is not None and (not is_int(scale) or scale < 0):
                raise err("PATCH_INVALID", "scale 必须为非负整数", path=item_path)
        elif op == "round":
            _require_field(item, item_path)
            scale = item.get("scale")
            if not is_int(scale) or scale < 0:
                raise err("PATCH_INVALID", "round.scale 必须为非负整数", path=item_path)


def _require_field(item: dict[str, Any], path: str, key: str = "field") -> None:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise err("PATCH_INVALID", f"{key} 必须为非空字符串", path=path)


def evaluate_filter(document: dict[str, Any], fields: dict[str, Any]) -> bool:
    if not document:
        return True
    if "all" in document:
        return all(evaluate_filter(item, fields) for item in document["all"])
    if "any" in document:
        return any(evaluate_filter(item, fields) for item in document["any"])
    if "not" in document:
        return not evaluate_filter(document["not"], fields)

    field = document["field"]
    op = document["op"]
    exists = field in fields
    if op == "exists":
        return exists is bool(document["value"])
    if not exists:
        return False
    left = fields[field]
    right = document["value"]
    if op == "eq":
        return canonical_json(left) == canonical_json(right)
    if op == "neq":
        return canonical_json(left) != canonical_json(right)
    if op in {"gt", "gte", "lt", "lte"}:
        left_number = to_decimal(left)
        right_number = to_decimal(right)
        if op == "gt":
            return left_number > right_number
        if op == "gte":
            return left_number >= right_number
        if op == "lt":
            return left_number < right_number
        return left_number <= right_number
    if op == "in":
        serialized = canonical_json(left)
        return any(serialized == canonical_json(item) for item in right)
    if op == "contains":
        if isinstance(left, (list, str)):
            return right in left
        return False
    raise err("PATCH_INVALID", "未知过滤操作符", op=op)


def apply_transforms(
    fields: dict[str, Any], transforms: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = copy.deepcopy(fields)
    trace: list[dict[str, Any]] = []
    for index, item in enumerate(transforms):
        op = item["op"]
        before = canonical_json(result)
        if op == "set":
            result[item["field"]] = copy.deepcopy(item["value"])
        elif op == "rename":
            source = item["from"]
            if source in result:
                result[item["to"]] = result.pop(source)
        elif op == "copy":
            source = item["from"]
            if source in result:
                result[item["to"]] = copy.deepcopy(result[source])
        elif op == "delete":
            result.pop(item["field"], None)
        elif op == "default":
            if item["field"] not in result or result[item["field"]] is None:
                result[item["field"]] = copy.deepcopy(item["value"])
        elif op in {"add", "multiply"}:
            field = item["field"]
            if field not in result:
                raise err(
                    "MALFORMED_EVENT",
                    "数值变换引用了不存在的字段",
                    field=field,
                    step=index,
                )
            current = to_decimal(result[field])
            operand = to_decimal(item["value"])
            computed = current + operand if op == "add" else current * operand
            result[field] = deterministic_number(computed, item.get("scale"))
        elif op == "round":
            field = item["field"]
            if field not in result:
                raise err(
                    "MALFORMED_EVENT",
                    "round 引用了不存在的字段",
                    field=field,
                    step=index,
                )
            result[field] = deterministic_number(
                Decimal(str(result[field])), item["scale"]
            )
        after = canonical_json(result)
        trace.append(
            {
                "step": index,
                "op": op,
                "changed": before != after,
                "digest": _short_digest(after),
            }
        )
    return result, trace


def _short_digest(text: str) -> str:
    from .util import sha256_text

    return sha256_text(text)
