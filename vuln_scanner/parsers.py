"""JSON / CSV 安全解析与字段遍历。

- 只接收通过安全校验的文件路径；
- 非 UTF-8 编码或内容损坏的文件抛出 :class:`ParseFailure`，由扫描器记录；
- 解析器只读不写，绝不修改原始文件内容。
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Iterator


class ParseFailure(Exception):
    """文件无法按 UTF-8 解码或内容格式损坏。"""


def iter_json_fields(text: str, source_name: str) -> Iterator[tuple[str, str]]:
    """递归遍历 JSON 文本中的所有字符串值。

    生成 ``(字段路径, 字符串值)``。字段路径形如 ``users[0].remark``。
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseFailure(f"JSON 格式损坏: {exc.msg} (第 {exc.lineno} 行)") from exc

    def walk(node: object, path: str) -> Iterator[tuple[str, str]]:
        if isinstance(node, dict):
            for key in node:
                child_path = f"{path}.{key}" if path else str(key)
                yield from walk(node[key], child_path)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                yield from walk(item, f"{path}[{index}]")
        elif isinstance(node, str):
            yield path, node
        # 数字/布尔/None 不携带注入文本，跳过

    yield from walk(data, "")


def iter_csv_fields(text: str, source_name: str) -> Iterator[tuple[str, str]]:
    """遍历 CSV 文本中的每个单元格。

    字段路径形如 ``row[2].email``（行号从 1 开始，与数据行对应）。
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ParseFailure("CSV 缺少表头行")
    for row_index, row in enumerate(reader, start=1):
        for column, value in row.items():
            if value is None:
                # DictReader 在列数多于表头时以 None 为键，跳过
                continue
            yield f"row[{row_index}].{column}", value


def read_text_strict(path: Path) -> str:
    """按严格 UTF-8 读取文件，非 UTF-8 内容直接判定解析失败。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ParseFailure(f"文件读取失败: {exc}") from exc
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ParseFailure(f"非 UTF-8 编码文件: {exc}") from exc


def parse_file(path: Path) -> Iterator[tuple[str, str]]:
    """根据扩展名分派解析，生成 ``(字段路径, 字段值)``。"""
    text = read_text_strict(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        yield from iter_json_fields(text, path.name)
    elif suffix == ".csv":
        yield from iter_csv_fields(text, path.name)
    else:  # pragma: no cover - 扩展名已在扫描入口过滤
        raise ParseFailure(f"不支持的扩展名: {path.suffix}")
