"""校验辅助：严格整数判定与 JSON 字段行号的尽力定位。"""
from __future__ import annotations

import json
from typing import Any

from .errors import Issue


def is_strict_int(value: Any) -> bool:
    """布尔值不是合法整数（JSON 中 true/false 不得冒充 1/0）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def locate_line(text: str, key: str, service_hint: str | None = None) -> int | None:
    """在原始 JSON 文本中尽力定位字段所在行号（1 基）。

    采用逐行扫描，优先匹配同时包含服务标识与字段名的行，
    否则退化为首个包含字段名的行；找不到返回 None。
    """
    key_hits: list[int] = []
    combined_hits: list[int] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if f'"{key}"' in line:
            key_hits.append(lineno)
            if service_hint is not None and f'"{service_hint}"' in line:
                combined_hits.append(lineno)
    if combined_hits:
        return combined_hits[0]
    if key_hits:
        return key_hits[0]
    return None


def service_line(text: str, service_id: str) -> int | None:
    for lineno, line in enumerate(text.splitlines(), start=1):
        if f'"{service_id}"' in line:
            return lineno
    return None


def parse_json_file(path: str) -> tuple[Any, str]:
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        from .errors import E_POLICY, GovernanceError

        raise GovernanceError(
            E_POLICY,
            "策略 JSON 语法解析失败",
            [Issue("$", exc.msg, line=exc.lineno, value=exc.colno)],
        ) from exc
    return data, text
