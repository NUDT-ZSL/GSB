"""文件下载接口路径穿越自检扫描器。

判定规则
--------
- 高风险 (high)：响应体命中基目录外标记文件的内容（即越界文件被成功读出）。
- 低风险 (low)：服务明确拒绝越界访问（HTTP 4xx），记录状态码与原因文本；
  或请求正常完成但响应中不包含标记内容。
- 未知 (unknown)：连接失败 / 连接重置 / 超时等网络层异常，绝不并入安全。

仅使用 Python 标准库，要求 Python 3.10+。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from collections import Counter
from typing import Any

from . import __version__

# Windows 控制台/管道默认可能是 GBK，统一 UTF-8 以保证中文报告可读。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

VERDICTS = ("high", "low", "unknown")
_VERDICT_RANK = {"high": 0, "low": 1, "unknown": 2}
# 单次响应最多读取 1 MiB，防止异常服务返回超大响应拖垮扫描进程。
_MAX_BODY_BYTES = 1024 * 1024


# --------------------------------------------------------------------------- #
# 输入加载
# --------------------------------------------------------------------------- #
def load_paths(json_path: str) -> list[str]:
    """从 JSON 文件读取测试路径。

    支持两种确定性结构：
      1. 纯数组：["a.txt", "../b.txt"]
      2. 对象形式：{"paths": ["a.txt", ...]}

    路径必须为非空字符串。文件缺失 / 无法解析 / 结构不合法时抛出
    ``InputError``，由调用方输出可读错误并以非零状态退出。
    """
    if not os.path.isfile(json_path):
        raise InputError(f"JSON 路径文件不存在或不是普通文件: {json_path}")
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise InputError(
            f"JSON 路径文件无法解析: {json_path}（第 {exc.lineno} 行第 {exc.colno} 列: {exc.msg}）"
        ) from exc
    except OSError as exc:
        raise InputError(f"JSON 路径文件无法读取: {json_path}（{exc}）") from exc

    if isinstance(data, list):
        raw_paths = data
    elif isinstance(data, dict) and isinstance(data.get("paths"), list):
        raw_paths = data["paths"]
    else:
        raise InputError(
            f"JSON 路径文件结构不合法: {json_path}，应为路径数组或含 paths 数组的对象"
        )

    paths: list[str] = []
    for idx, item in enumerate(raw_paths):
        if not isinstance(item, str) or not item:
            raise InputError(
                f"JSON 路径文件第 {idx} 项不是非空字符串，实际类型: {type(item).__name__}"
            )
        paths.append(item)
    if not paths:
        raise InputError(f"JSON 路径文件中没有任何测试路径: {json_path}")
    return paths


class InputError(Exception):
    """用户输入错误（文件缺失、JSON 损坏、结构非法等）。"""


# --------------------------------------------------------------------------- #
# 单条探测
# --------------------------------------------------------------------------- #
def build_url(base_url: str, endpoint: str, raw_path: str) -> str:
    """拼接请求 URL。

    ``raw_path`` 作为 filename 的值逐字保留，不做任何百分号编码或改写：
    直接字符串拼接到查询串中（urllib 发送时原样保留 %xx 字节）。
    """
    base = base_url.rstrip("/")
    ep = endpoint if endpoint.startswith("/") else "/" + endpoint
    return f"{base}{ep}?filename={raw_path}"


def _read_body(resp: Any) -> bytes:
    try:
        return resp.read(_MAX_BODY_BYTES)
    except OSError:
        return b""


def probe(base_url: str, endpoint: str, raw_path: str,
          marker_bytes: bytes, timeout: float,
          marker_name: str | None = None) -> dict[str, Any]:
    """对单条测试路径发起下载请求并判定，返回一条明细记录。"""
    url = build_url(base_url, endpoint, raw_path)
    record: dict[str, Any] = {
        "test_path": raw_path,
        "url": url,
        "verdict": None,
        "http_status": None,
        "reason": None,
        "evidence": None,
        "error": None,
    }
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = _read_body(resp)
    except urllib.error.HTTPError as exc:
        # 服务端明确给出 HTTP 错误响应（如 403 拒绝越界），仍然可读响应体。
        status = exc.code
        try:
            body = exc.read(_MAX_BODY_BYTES)
        except OSError:
            body = b""
        finally:
            exc.close()
        reason_header = exc.headers.get("X-Reject-Reason") if exc.headers else None
        record["reason"] = reason_header or _decode(body) or exc.reason
    except (urllib.error.URLError, socket.timeout, TimeoutError,
            ConnectionError, OSError) as exc:
        # 连接失败 / 重置 / 超时 —— 判定未知，绝不并入安全。
        record["verdict"] = "unknown"
        record["error"] = _describe_error(exc)
        return record

    record["http_status"] = status
    if marker_bytes and marker_bytes in body:
        record["verdict"] = "high"
        record["evidence"] = {
            "type": "marker_content_match",
            "marker_file_name": marker_name,
            "marker_bytes_sha_note": "响应体包含基目录外标记文件的完整内容",
            "matched_marker_excerpt": _decode(marker_bytes),
            "response_excerpt": _decode(body[:200]),
        }
        if record["reason"] is None:
            record["reason"] = f"HTTP {status} 响应体命中基目录外标记文件内容"
    else:
        record["verdict"] = "low"
        if record["reason"] is None:
            record["reason"] = f"HTTP {status} 响应未包含标记文件内容"
    return record


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _describe_error(exc: BaseException) -> str:
    """生成不依赖系统代码页的稳定英文错误描述。

    Windows 中文系统下 socket/OSError 的 str() 可能是 GBK 本地化文本，
    写入 UTF-8 报告后在其他环境复核会乱码，因此按异常类型分类描述，
    并附上 errno 等结构化信息而非本地化消息原文。
    """
    reason = getattr(exc, "reason", None)
    if isinstance(reason, socket.timeout):
        return "socket.timeout: request timed out"
    if isinstance(reason, ConnectionRefusedError):
        return (f"ConnectionRefusedError: [errno {reason.errno}] "
                f"connection refused by target")
    if isinstance(reason, ConnectionResetError):
        return (f"ConnectionResetError: [errno {reason.errno}] "
                f"connection reset by target")
    if isinstance(reason, OSError):
        return f"{type(reason).__name__}: [errno {reason.errno}] network error"
    if isinstance(exc, socket.timeout):
        return "socket.timeout: request timed out"
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, OSError)):
        return f"{type(exc).__name__}: [errno {exc.errno}] network error"
    if isinstance(exc, urllib.error.URLError) and reason is not None:
        return f"{type(reason).__name__}: network error"
    return f"{type(exc).__name__}: request failed"


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 高风险 -> 低风险 -> 未知 排序，同层级内按测试路径字母序稳定排列。"""
    return sorted(
        records,
        key=lambda r: (_VERDICT_RANK[r["verdict"]], r["test_path"]),
    )


def build_report(base_url: str, endpoint: str,
                 records: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sort_records(records)
    counts = Counter(r["verdict"] for r in ordered)
    summary = {v: counts.get(v, 0) for v in VERDICTS}
    summary["total"] = len(ordered)
    return {
        "tool": "pathscan",
        "version": __version__,
        "target": {"base_url": base_url.rstrip("/"), "endpoint": endpoint},
        "summary": summary,
        "details": ordered,
    }


def write_report(report: dict[str, Any], output_path: str) -> None:
    """原子写入合法 UTF-8 JSON：先写同目录临时文件再替换，避免残缺报告。"""
    parent = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".report-", suffix=".tmp", dir=parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp_name, output_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def run_scan(base_url: str, endpoint: str, paths: list[str],
             marker_path: str, timeout: float) -> dict[str, Any]:
    """加载标记并逐条探测，组装最终报告。"""
    try:
        with open(marker_path, "rb") as fh:
            marker_bytes = fh.read()
    except OSError as exc:
        raise InputError(f"标记文件无法读取: {marker_path}（{exc}）") from exc
    if not marker_bytes:
        raise InputError(f"标记文件为空，无法作为命中依据: {marker_path}")

    marker_name = os.path.basename(marker_path)
    records = [
        probe(base_url, endpoint, raw_path, marker_bytes, timeout,
              marker_name=marker_name)
        for raw_path in paths
    ]
    return build_report(base_url, endpoint, records)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pathscan",
        description="文件下载接口路径穿越自检工具（标准库实现）",
    )
    parser.add_argument(
        "-u", "--base-url", required=True,
        help="目标基地址，例如 http://127.0.0.1:8081",
    )
    parser.add_argument(
        "-e", "--endpoint", default="/download",
        help="接口路径，默认 /download",
    )
    parser.add_argument(
        "-p", "--path", action="append", default=[],
        help="测试路径（作为 filename 原始值，不做二次编码）；可重复指定",
    )
    parser.add_argument(
        "-f", "--paths-file", action="append", default=[],
        help="JSON 路径文件；可重复指定，与 --path 合并按出现顺序探测",
    )
    parser.add_argument(
        "-m", "--marker", required=True,
        help="基目录外标记文件路径，其内容作为高风险命中依据",
    )
    parser.add_argument(
        "-o", "--output", default="report.json",
        help="报告输出路径，默认当前目录 report.json",
    )
    parser.add_argument(
        "-t", "--timeout", type=float, default=5.0,
        help="单条请求超时秒数，默认 5.0",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # 所有输入校验在发起任何网络请求 / 写任何报告之前完成，
    # 输入不合法时直接非零退出，不产生残缺报告。
    try:
        paths: list[str] = list(args.path)
        for json_file in args.paths_file:
            paths.extend(load_paths(json_file))
        if not paths:
            raise InputError("未提供任何测试路径：请用 --path 或 --paths-file 指定")
        report = run_scan(
            args.base_url, args.endpoint, paths, args.marker, args.timeout
        )
        write_report(report, args.output)
    except InputError as exc:
        print(f"pathscan: 错误: {exc}", file=sys.stderr)
        return 2

    summary = report["summary"]
    print(
        "扫描完成: 总计 {total} 条 —— 高风险 {high} / 低风险 {low} / 未知 {unknown}；"
        "报告已写入 {out}".format(out=args.output, **summary)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
