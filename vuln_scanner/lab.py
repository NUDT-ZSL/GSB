"""生成确定性的本地扫描实验室：目标清单、策略、非法输入样例。

用法：
    python -m vuln_scanner.lab --lab-dir lab --port 8765
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .config import DEFAULT_ENDPOINTS, ALL_CHECKS


def _endpoints_json():
    return [
        {"path": ep.path, "checks": list(ep.checks),
         "parameter": ep.parameter, "placeholder": ep.placeholder}
        for ep in DEFAULT_ENDPOINTS
    ]


def build_targets(port: int) -> dict:
    return {
        "targets": [
            {
                "name": "local-vulnerable-sample",
                "base_url": "http://127.0.0.1:%d" % port,
                "endpoints": _endpoints_json(),
            },
            {
                # 本机大概率未监听的端口：连接被拒绝，用于需求 4 验收
                "name": "unreachable-target",
                "base_url": "http://127.0.0.1:9",
                "endpoints": _endpoints_json(),
            },
        ]
    }


def build_policy() -> dict:
    return {
        "timeout_seconds": 2.0,
        "enabled_checks": list(ALL_CHECKS),
    }


def build_invalid_url_targets(port: int) -> dict:
    return {
        "targets": [
            {"name": "bad-scheme", "base_url": "ftp://127.0.0.1:%d/" % port},
            {"name": "no-scheme", "base_url": "127.0.0.1:%d/" % port},
            {"name": "javascript-uri",
             "base_url": "javascript:alert(1)"},
        ]
    }


def build_missing_field_targets() -> dict:
    return {"targets": [{"name": "missing-base-url"}]}


def build_bad_policy() -> dict:
    return {"timeout_seconds": 2.0}  # 缺 enabled_checks


def generate(lab_dir: str, port: int) -> list[str]:
    root = Path(lab_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "invalid").mkdir(exist_ok=True)

    files = {
        "targets.json": build_targets(port),
        "policy.json": build_policy(),
        "policy_no_sqli.json": {
            "timeout_seconds": 2.0,
            "enabled_checks": ["xss", "path_traversal"],
        },
        "invalid/targets_bad_url.json": build_invalid_url_targets(port),
        "invalid/targets_missing_field.json": build_missing_field_targets(),
        "invalid/policy_missing_field.json": build_bad_policy(),
    }
    written = []
    for rel, payload in files.items():
        path = root / rel
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        written.append(str(path))
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成本地扫描实验室文件")
    parser.add_argument("--lab-dir", default="lab")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("SAMPLE_PORT", "8765")))
    args = parser.parse_args(argv)
    for path in generate(args.lab_dir, args.port):
        print("generated:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
