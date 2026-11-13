"""三个异构数据源适配器。

统一接口：fetch(cursor, limit) -> FetchResult
- REST 源：页码游标，回传服务端声明总页数
- 时间戳源：毫秒级半开区间 [start, end)，end 作为下一次 start
- CSV 源：完整文件内容摘要识别快照版本；稳定版本内使用字节行偏移；
  新版本全量重扫；检测部分写入并返回 should_retry，残缺行不计入批次。

发布契约：所有完整 CSV 行以换行结尾。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from typing import Any

import requests

from .errors import (
    AuthError, BadRequestError, DecodeError, NetError, RateLimitedError,
    ServerError, ValidationError, CsvPartialError,
)

REQUIRED_FIELDS = ("serial_no", "amount", "customer", "event_ts", "detail_code")


@dataclass
class FetchResult:
    records: list[dict]
    next_cursor: Any
    exhausted: bool
    should_retry: bool = False
    status: str = "ok"                # ok | should_retry | error
    error_code: str | None = None
    extra: dict = field(default_factory=dict)


def _validate_record(record: dict) -> None:
    if not isinstance(record, dict):
        raise ValidationError("记录不是 JSON 对象")
    missing = [f for f in REQUIRED_FIELDS if f not in record]
    if missing:
        raise ValidationError("记录缺少必填字段: %s" % ",".join(missing))
    if not isinstance(record["serial_no"], str) or not record["serial_no"]:
        raise ValidationError("serial_no 必须是非空字符串")


def _raise_http_error(status: int) -> None:
    if status == 429:
        raise RateLimitedError()
    if 500 <= status < 600:
        raise ServerError("HTTP %d" % status)
    if status == 401:
        raise AuthError()
    if status == 400:
        raise BadRequestError()
    if status in (403, 404):
        raise BadRequestError("HTTP %d" % status, code={403: "E_FORBIDDEN", 404: "E_NOT_FOUND"}[status])
    # 其他未预期状态码按不可重试业务错误处理
    raise BadRequestError("HTTP %d" % status, code="E_HTTP_%d" % status)


class BaseAdapter:
    source = "base"
    cursor_type = "page"

    def initial_cursor(self) -> Any:
        raise NotImplementedError

    def fetch(self, cursor: Any, limit: int) -> FetchResult:
        raise NotImplementedError

    def close(self) -> None:
        pass


class RestAdapter(BaseAdapter):
    """源 A：本地模拟 REST 分页接口（页码游标）。"""

    source = "rest"
    cursor_type = "page"

    def __init__(self, base_url: str, session: requests.Session | None = None,
                 timeout: float = 10.0):
        self.base_url = base_url
        self.session = session or requests.Session()
        self.timeout = timeout
        self._total_pages: int | None = None

    def initial_cursor(self) -> int:
        return 1

    def fetch(self, cursor: Any, limit: int) -> FetchResult:
        page = int(cursor)
        try:
            resp = self.session.get(
                self.base_url,
                params={"page": page, "limit": limit},
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise NetError(str(exc)) from exc
        if resp.status_code != 200:
            _raise_http_error(resp.status_code)
        try:
            doc = resp.json()
        except (json.JSONDecodeError, requests.exceptions.JSONDecodeError) as exc:
            raise DecodeError(str(exc)) from exc
        if not isinstance(doc, dict) or "records" not in doc:
            raise ValidationError("REST 响应结构非法")
        records = doc["records"]
        if not isinstance(records, list):
            raise ValidationError("REST records 不是数组")
        for record in records:
            _validate_record(record)
        total_pages = int(doc["total_pages"])
        self._total_pages = total_pages
        exhausted = page >= total_pages
        next_cursor = page + 1 if not exhausted else total_pages + 1
        return FetchResult(
            records=records,
            next_cursor=next_cursor,
            exhausted=exhausted,
            extra={"total_pages": total_pages, "page": page},
        )

    def close(self) -> None:
        self.session.close()


class TimestampAdapter(BaseAdapter):
    """源 B：毫秒级半开区间 [start, end) 增量接口。"""

    source = "ts"
    cursor_type = "ts"

    def __init__(self, base_url: str, window_ms: int = 100,
                 session: requests.Session | None = None, timeout: float = 10.0):
        self.base_url = base_url
        self.window_ms = window_ms
        self.session = session or requests.Session()
        self.timeout = timeout
        # 与生成器基准对齐；首次游标可由外部显式设置
        self._start_default: int | None = None

    def initial_cursor(self) -> dict:
        # 从生成器基准开始；测试也可直接传入具体游标
        from .generator import TS_BASE
        return {"start": TS_BASE, "end": TS_BASE + self.window_ms}

    def fetch(self, cursor: Any, limit: int) -> FetchResult:
        if isinstance(cursor, dict):
            start = int(cursor["start"])
            end = int(cursor["end"])
        else:  # 持久化恢复时游标也可能被压扁为整数 start
            start = int(cursor)
            end = start + self.window_ms
        try:
            resp = self.session.get(
                self.base_url,
                params={"start": start, "end": end, "limit": limit},
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise NetError(str(exc)) from exc
        if resp.status_code != 200:
            _raise_http_error(resp.status_code)
        try:
            doc = resp.json()
        except (json.JSONDecodeError, requests.exceptions.JSONDecodeError) as exc:
            raise DecodeError(str(exc)) from exc
        if not isinstance(doc, dict) or "records" not in doc:
            raise ValidationError("TS 响应结构非法")
        records = doc["records"]
        for record in records:
            _validate_record(record)
        max_seen = max((r["event_ts"] for r in records), default=None)
        # 空窗口且已越过数据上界 -> 结束（由引擎依据空窗计数推进）
        return FetchResult(
            records=records,
            next_cursor={"start": end, "end": end + self.window_ms},
            exhausted=False,
            extra={"start": start, "end": end,
                   "empty": len(records) == 0,
                   "max_seen_ts": max_seen,
                   "truncated": bool(doc.get("truncated"))},
        )


class CsvAdapter(BaseAdapter):
    """源 C：CSV 快照文件（全量覆盖写，可能被部分写入）。"""

    source = "csv"
    cursor_type = "offset"

    def __init__(self, path: str):
        self.path = path
        self._version: str | None = None
        self._header: list[str] | None = None

    def initial_cursor(self) -> dict:
        return {"version": None, "offset": 0}

    def _read_bytes(self) -> bytes:
        with open(self.path, "rb") as fh:
            return fh.read()

    @staticmethod
    def digest(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def fetch(self, cursor: Any, limit: int) -> FetchResult:
        cursor = dict(cursor) if isinstance(cursor, dict) else {"version": None, "offset": 0}
        try:
            content = self._read_bytes()
        except OSError as exc:
            raise CsvPartialError("CSV 读取失败: %s" % exc) from exc
        version = self.digest(content)

        # ---- 部分写入检测（优先于版本切换，保证游标原样回传） ----
        # 发布契约要求所有完整行以换行结尾；末尾无换行 => 最后一行不完整
        partial = False
        partial_reason = None
        if not content.endswith(b"\n"):
            partial = True
            partial_reason = "last_line_without_newline"

        raw_lines = content.splitlines(keepends=True)
        line_offsets: list[int] = []
        acc = 0
        for line in raw_lines:
            line_offsets.append(acc)
            acc += len(line)
        total_len = len(content)

        # 仅完整行（带换行）参与解析；残缺行不计入批次
        complete_lines: list[tuple[int, bytes]] = []
        for line_start, line in zip(line_offsets, raw_lines):
            if line.endswith(b"\n"):
                complete_lines.append((line_start, line))

        if not complete_lines:
            raise CsvPartialError("CSV 无任何完整行")

        header_line = complete_lines[0][1].decode("utf-8").rstrip("\n").rstrip("\r")
        header = next(csv.reader(io.StringIO(header_line)))
        expected_cols = len(header)
        self._header = header

        parsed_rows: list[dict] = []
        valid_offsets: list[int] = []
        for line_start, line in complete_lines[1:]:
            line_text = line.decode("utf-8").rstrip("\n").rstrip("\r")
            if line_text == "":
                continue
            values = next(csv.reader(io.StringIO(line_text)))
            if len(values) != expected_cols:
                partial = True
                partial_reason = "field_count_mismatch"
                break
            row = dict(zip(header, values))
            try:
                row["amount"] = float(row["amount"])
                row["event_ts"] = int(row["event_ts"])
            except (KeyError, ValueError) as exc:
                raise ValidationError("CSV 字段类型非法: %s" % exc) from exc
            _validate_record(row)
            parsed_rows.append(row)
            valid_offsets.append(line_start + len(line))

        if partial:
            # 不推进版本/偏移，残缺行不计入批次，原样回传调用方游标
            return FetchResult(
                records=[],
                next_cursor={"version": cursor.get("version"),
                             "offset": int(cursor.get("offset", 0))},
                exhausted=False,
                should_retry=True,
                status="should_retry",
                error_code="E_CSV_PARTIAL",
                extra={"reason": partial_reason},
            )

        # ---- 完整快照：新版本（含首次）全量重扫，不沿用旧版本行偏移 ----
        if cursor.get("version") != version:
            cursor = {"version": version, "offset": 0}
        offset = int(cursor["offset"])

        # ---- 稳定版本内按行偏移分页 ----
        start_index = 0
        for i, end_off in enumerate(valid_offsets):
            if end_off > offset:
                start_index = i
                break
        else:
            start_index = len(parsed_rows)
        batch = parsed_rows[start_index:start_index + limit]
        new_offset = valid_offsets[start_index + len(batch) - 1] if batch else offset
        exhausted = (start_index + len(batch)) >= len(parsed_rows) and len(batch) > 0
        if not batch and start_index >= len(parsed_rows):
            exhausted = True
            new_offset = total_len
        return FetchResult(
            records=batch,
            next_cursor={"version": version, "offset": new_offset},
            exhausted=exhausted,
            extra={"version": version, "header": header,
                   "total_rows": len(parsed_rows)},
        )
