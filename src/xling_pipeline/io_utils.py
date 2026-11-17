"""JSONL 读取与临时文件 + os.replace 原子写盘工具。"""

import json
import os


def read_jsonl(path):
    """逐行读取 JSONL；空行跳过，非法 JSON 抛出带行号的异常。"""
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
    return rows


def atomic_write_json(path, payload, indent=2):
    """同目录临时文件写入并原子替换，保证快照/报告永不出现半写状态。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}"
    text = json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=False)
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    return path


def canonical_dumps(payload):
    """确定性 JSON 字符串（排序键、固定分隔符），用于快照内摘要比较。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
