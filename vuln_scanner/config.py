"""本地自定义检测规则配置加载。

配置为 JSON 文件；配置文件本身缺失、编码错误或 JSON 语法错误时，
报告具体问题，但内置规则仍然生效，扫描照常完成。
"""

from __future__ import annotations

import json
from pathlib import Path

from .rules import Rule, load_custom_rules


def load_config(config_path: str | Path | None) -> tuple[list[Rule], list[str]]:
    """加载自定义规则配置。

    返回 ``(自定义规则列表, 配置问题列表)``。任何配置问题都不会抛出，
    而是以问题字符串的形式返回，保证其余检测不受影响。
    """
    if not config_path:
        return [], []
    path = Path(config_path)
    if not path.exists():
        return [], [f"配置文件不存在: {path}"]
    try:
        raw = path.read_bytes().decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        return [], [f"配置文件非 UTF-8 编码: {exc}"]
    except OSError as exc:
        return [], [f"配置文件读取失败: {exc}"]
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], [f"配置文件 JSON 语法错误: {exc.msg} (第 {exc.lineno} 行)"]
    if not isinstance(config, dict):
        return [], ["配置文件顶层必须是 JSON 对象，自定义规则未加载"]
    return load_custom_rules(config)
