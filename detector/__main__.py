"""检测命令入口：``python -m detector``。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from authsvc.constants import DEFAULT_SHARED_TOKEN

from .core import Report, detect


def _write_json(path: str | None, data: dict) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
    print(text)


def _report_payload(report: Report) -> dict:
    return report.to_dict()


def _compare_reports(legacy: Report, fixed: Report) -> dict:
    legacy_payload = legacy.to_dict()
    fixed_payload = fixed.to_dict()
    legacy_checks = {check["id"]: check for check in legacy_payload["checks"]}
    fixed_checks = {check["id"]: check for check in fixed_payload["checks"]}
    rows = []
    for check_id in legacy_checks:
        old = legacy_checks[check_id]
        new = fixed_checks.get(check_id, {})
        rows.append(
            {
                "id": check_id,
                "category": old["category"],
                "legacy_status": old["status"],
                "legacy_conclusion": old["conclusion"],
                "fixed_status": new.get("status"),
                "fixed_conclusion": new.get("conclusion"),
                "resolved": old["status"] == "vulnerability" and new.get("status") == "pass",
            }
        )
    return {
        "report_schema_version": legacy_payload["report_schema_version"],
        "tool": "authsvc-detector",
        "mode": "comparison",
        "legacy": legacy_payload,
        "fixed": fixed_payload,
        "comparison": {
            "checks": rows,
            "legacy_overall": legacy_payload["overall"],
            "fixed_overall": fixed_payload["overall"],
            "legacy_vulnerabilities": [
                c["id"] for c in legacy_payload["checks"] if c["status"] == "vulnerability"
            ],
            "fixed_vulnerabilities": [
                c["id"] for c in fixed_payload["checks"] if c["status"] == "vulnerability"
            ],
            "all_differences_resolved": all(row["resolved"] for row in rows),
        },
    }


def _scan_command(args: argparse.Namespace) -> int:
    report = detect(args.base_url, token=args.token, label=args.label)
    payload = _report_payload(report)
    _write_json(args.out, payload)
    if not report.clean:
        return 1
    return 0


def _compare_command(args: argparse.Namespace) -> int:
    # 两版服务按顺序独立检测：每次 detect() 只使用自己的 base_url，
    # 报告对象互不共享状态，因此任一版的结论不受另一版影响。
    legacy = detect(args.legacy_url, token=args.token, label=args.legacy_label)
    fixed = detect(args.fixed_url, token=args.token, label=args.fixed_label)
    if args.legacy_out:
        _write_json(args.legacy_out, _report_payload(legacy))
    if args.fixed_out:
        _write_json(args.fixed_out, _report_payload(fixed))
    comparison = _compare_reports(legacy, fixed)
    _write_json(args.out, comparison)
    # 对比模式不把某一版的结论当作退出码；报告本身给出两版结论。
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="detector",
        description="本地授权账目服务未授权访问探测器（仅探测本机回环地址）",
    )
    parser.add_argument("--token", default=DEFAULT_SHARED_TOKEN, help="合法共享令牌")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="探测单个运行中的服务")
    scan.add_argument("--base-url", required=True)
    scan.add_argument("--label", default=None)
    scan.add_argument("--out", default=None, help="把 JSON 报告写入文件")
    scan.set_defaults(func=_scan_command)

    compare = subparsers.add_parser("compare", help="先后独立探测新旧两版服务")
    compare.add_argument("--legacy-url", required=True)
    compare.add_argument("--fixed-url", required=True)
    compare.add_argument("--legacy-label", default="legacy")
    compare.add_argument("--fixed-label", default="fixed")
    compare.add_argument("--legacy-out", default=None)
    compare.add_argument("--fixed-out", default=None)
    compare.add_argument("--out", default=None, help="对比报告输出路径")
    compare.set_defaults(func=_compare_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError) as exc:
        # 目标未启动、地址非法或不是本地 HTTP 服务：给出明确原因而不是原始栈。
        print(f"detector: 无法完成探测: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
