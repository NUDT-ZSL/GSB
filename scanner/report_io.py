"""确定性 JSON 读写与报告落盘工具。

保证同一份报告多次运行逐字节一致：
- ensure_ascii=False 固定、sort_keys=True 固定、缩进固定、分隔符固定；
- 行尾统一为 LF，文件以无 BOM 的 UTF-8 写入；
- 报告不包含时间戳、机器名、随机数等非确定内容。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

INDENT = 2


def dumps_deterministic(report: dict[str, Any]) -> str:
    return json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        indent=INDENT,
        separators=(",", ": "),
    )


def write_report(report: dict[str, Any], output_path: Path) -> None:
    text = dumps_deterministic(report)
    # newline="\n" 强制 LF，避免 Windows 上被改写为 CRLF 破坏逐字节一致性
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.write("\n")


def read_report(input_path: Path) -> dict[str, Any]:
    with input_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
