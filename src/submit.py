# -*- coding: utf-8 -*-
"""业务操作样例：运维提交清单，修复后组件逐条归档或拒绝。

用法：
    python src/submit.py --archive-root testroot/archive --manifest testroot/manifests/all_safe.json

每条打印归档结果；存在被拒绝条目时退出码为 1。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from archive_service import ArchiveRejected, FixedArchiver  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description="提交清单并归档（修复后组件）")
    parser.add_argument("--archive-root", required=True)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)

    with open(args.manifest, "r", encoding="utf-8") as f:
        entries = json.load(f)

    archiver = FixedArchiver(args.archive_root)
    rejected = 0
    for entry in entries:
        filename = entry.get("filename") if isinstance(entry, dict) else None
        content = entry.get("content") if isinstance(entry, dict) else None
        try:
            path = archiver.archive(filename, content)
            print(f"[归档成功] {filename} -> {path}")
        except ArchiveRejected as exc:
            rejected += 1
            print(f"[拒绝] {filename!r}：{exc}")
    return 1 if rejected else 0


if __name__ == "__main__":
    sys.exit(main())
