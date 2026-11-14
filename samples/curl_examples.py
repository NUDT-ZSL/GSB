#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""可复现的业务样例：分别向两个本地实例发送三条附件预览请求并打印结果。

仅访问 127.0.0.1 上的实例；不写入报告，仅用于人工核对。
用法：python samples/curl_examples.py
"""
from __future__ import annotations

import sys
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MARKER = str(PROJECT_ROOT / "outside_attachments" / "secret_marker.txt")

SAMPLES = [
    ("正常附件名", "员工手册-2026.txt"),
    ("目录穿越", "../outside_attachments/secret_marker.txt"),
    ("绝对路径", MARKER),
]

INSTANCES = [
    ("漏洞版", "http://127.0.0.1:5000"),
    ("修复版", "http://127.0.0.1:5001"),
]


def fetch(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return -1, f"实例不可达: {exc}"


def main() -> int:
    for label, base in INSTANCES:
        print(f"\n=== {label} {base} ===")
        for sample_label, name in SAMPLES:
            url = f"{base}/api/attachment/preview?{urllib.parse.urlencode({'name': name})}"
            status, body = fetch(url)
            text = " ".join(body.split())[:100]
            print(f"[{sample_label}] {url}\n  status={status} body: {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
