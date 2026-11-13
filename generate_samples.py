#!/usr/bin/env python3
"""generate_samples.py — 确定性生成 .authcfg 测试样本集（仅标准库）。

用法:
    python generate_samples.py --dir <样本目录>

同一参数重复运行产出逐字节一致的内容（无随机、无时间戳）。
"""
from __future__ import annotations

import argparse
import json
import os

# 合法样本
SAFE = {
    "version": 2,
    "subject": "alice",
    "resource_path": "data/reports/q3.txt",
    "max_connections": 64,
    "timeout_ms": 5000,
}

# 路径越界：上跳到样本目录之外
PATH_TRAVERSAL = {
    "version": 2,
    "subject": "mallory",
    "resource_path": "../../outside/secret.txt",
    "max_connections": 10,
    "timeout_ms": 1000,
}

# 数值越界：负数
NUMERIC_NEGATIVE = {
    "version": 1,
    "subject": "bob",
    "resource_path": "data/ok.txt",
    "max_connections": -1,
    "timeout_ms": 1000,
}

# 数值越界：超过上限
NUMERIC_OVERFLOW = {
    "version": 1,
    "subject": "carol",
    "resource_path": "data/ok.txt",
    "max_connections": 100,
    "timeout_ms": 999999999999,
}

# 畸形：缺失必需字段 resource_path
MALFORMED_MISSING = {
    "version": 2,
    "subject": "dave",
    "max_connections": 5,
    "timeout_ms": 500,
}

# 畸形：字段类型不符
MALFORMED_TYPE = {
    "version": 2,
    "subject": "erin",
    "resource_path": "data/ok.txt",
    "max_connections": "many",
    "timeout_ms": 500,
}

# 畸形：非法 JSON
MALFORMED_JSON = b'{ "version": 2, "subject": '


def _dump(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build_samples() -> dict:
    """返回 {文件名: 字节内容}，键序即写入顺序（与检测报告顺序无关）。"""
    oversized = dict(SAFE)
    oversized["subject"] = "frank"
    oversized["padding"] = "A" * (1536 * 1024)  # 1.5MB 填充，超过 1MB 上限
    return {
        "01_safe.authcfg": _dump(SAFE),
        "02_path_traversal.authcfg": _dump(PATH_TRAVERSAL),
        "03_numeric_negative.authcfg": _dump(NUMERIC_NEGATIVE),
        "04_numeric_overflow.authcfg": _dump(NUMERIC_OVERFLOW),
        "05_malformed_missing.authcfg": _dump(MALFORMED_MISSING),
        "06_malformed_type.authcfg": _dump(MALFORMED_TYPE),
        "07_malformed_json.authcfg": MALFORMED_JSON,
        "08_oversize.authcfg": _dump(oversized),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Deterministically generate .authcfg samples.")
    parser.add_argument("--dir", required=True, help="样本输出目录（不存在则创建）")
    args = parser.parse_args(argv)

    os.makedirs(args.dir, exist_ok=True)
    samples = build_samples()
    for name, content in samples.items():
        with open(os.path.join(args.dir, name), "wb") as fh:
            fh.write(content)
    # 一个非样本文件，验证扫描器会忽略它
    with open(os.path.join(args.dir, "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("this file is not a sample and must be ignored\n")

    print("generated %d sample(s) + 1 non-sample file in: %s"
          % (len(samples), os.path.abspath(args.dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
