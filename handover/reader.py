"""单个 JSONL 文件的读取与异常识别。

文件状态：
- ok:        完整读取，逐行均为合法 JSON 对象；
- locked:    文件被其他进程占用（Windows 共享违例，errno EACCES/EAGAIN）；
- truncated: 存在无法解析的 JSON 行或非对象行，视为写入半截；
- missing:   文件已不存在（续跑时用于和历史状态对账）。

被占用 / 半截文件的记录全部不进入本批次，保证单文件原子性；
其他文件照常处理（需求 5）。
"""

from __future__ import annotations

import errno as errno_mod
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

STATUS_OK = "ok"
STATUS_LOCKED = "locked"
STATUS_TRUNCATED = "truncated"
STATUS_MISSING = "missing"
STATUS_SOURCE_UNMATCHED = "source_unmatched"

_TERMINAL_STATUSES = (
    STATUS_LOCKED,
    STATUS_TRUNCATED,
    STATUS_MISSING,
    STATUS_SOURCE_UNMATCHED,
)


@dataclass
class RawRecord:
    line_no: int
    data: dict


@dataclass
class FileReadResult:
    file_name: str
    status: str
    records: list[RawRecord] = field(default_factory=list)
    detail: str = ""          # 非 ok 状态的人类可读说明
    bad_line_no: int | None = None
    bad_line_preview: str = ""
    size_bytes: int = 0


def _classify_open_error(exc: OSError, path: Path) -> FileReadResult:
    # Windows: 共享违例 WinError 32（被占用）/ 33（锁违例）
    winerror = getattr(exc, "winerror", None)
    if winerror in (32, 33) or exc.errno in (errno_mod.EACCES, errno_mod.EAGAIN):
        return FileReadResult(
            file_name=path.name,
            status=STATUS_LOCKED,
            detail=f"文件被占用，无法读取（{type(exc).__name__}: {exc}）",
        )
    return FileReadResult(
        file_name=path.name,
        status=STATUS_LOCKED,
        detail=f"文件无法打开（{type(exc).__name__}: {exc}）",
    )


def read_jsonl_file(path: str | Path) -> FileReadResult:
    """读取一个 JSONL 文件，返回结构化读取结果（不抛出业务异常）。"""
    p = Path(path)
    try:
        size = p.stat().st_size
    except FileNotFoundError:
        return FileReadResult(file_name=p.name, status=STATUS_MISSING,
                              detail="文件不存在")
    except OSError as exc:
        result = _classify_open_error(exc, p)
        result.size_bytes = 0
        return result

    try:
        # 先以独占式读打开探测写锁；失败则按平台错误码判定占用
        try:
            probe = open(p, "r+b")
            probe.close()
        except OSError as exc:
            result = _classify_open_error(exc, p)
            result.size_bytes = size
            return result

        with open(p, "r", encoding="utf-8-sig") as fh:
            content = fh.read()
    except OSError as exc:
        result = _classify_open_error(exc, p)
        result.size_bytes = size
        return result

    records: list[RawRecord] = []
    for line_no, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            preview = line.strip()[:120]
            return FileReadResult(
                file_name=p.name,
                status=STATUS_TRUNCATED,
                detail=f"第 {line_no} 行不是合法 JSON（{exc.msg}），文件疑似只写入了一半",
                bad_line_no=line_no,
                bad_line_preview=preview,
                size_bytes=size,
            )
        if not isinstance(value, dict):
            return FileReadResult(
                file_name=p.name,
                status=STATUS_TRUNCATED,
                detail=f"第 {line_no} 行不是 JSON 对象，文件疑似只写入了一半",
                bad_line_no=line_no,
                bad_line_preview=json.dumps(value, ensure_ascii=False)[:120],
                size_bytes=size,
            )
        records.append(RawRecord(line_no=line_no, data=value))

    return FileReadResult(
        file_name=p.name,
        status=STATUS_OK,
        records=records,
        size_bytes=size,
    )


def is_unfinished(status: str) -> bool:
    """该状态是否属于报告中的“未完成”类。"""
    return status in _TERMINAL_STATUSES
