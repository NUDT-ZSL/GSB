"""端到端演示：依次启动旧版与修复版服务，分别检测并对比报告。

运行：``python examples/demo.py``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from authsvc.constants import DEFAULT_SHARED_TOKEN  # noqa: E402
from detector.__main__ import _compare_reports  # noqa: E402
from detector.core import detect  # noqa: E402
from examples.harness import start_service  # noqa: E402


def main() -> int:
    reports_dir = PROJECT_ROOT / "reports"
    reports_dir.mkdir(exist_ok=True)

    print("== 启动旧版服务 ==")
    legacy = start_service("legacy")
    print(f"legacy base_url = {legacy.base_url}")
    try:
        legacy_report = detect(legacy.base_url, DEFAULT_SHARED_TOKEN, label="legacy")
    finally:
        legacy.stop()

    print("== 启动修复版服务 ==")
    fixed = start_service("fixed")
    print(f"fixed base_url = {fixed.base_url}")
    try:
        fixed_report = detect(fixed.base_url, DEFAULT_SHARED_TOKEN, label="fixed")
    finally:
        fixed.stop()

    comparison = _compare_reports(legacy_report, fixed_report)
    (reports_dir / "legacy_report.json").write_text(
        json.dumps(legacy_report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (reports_dir / "fixed_report.json").write_text(
        json.dumps(fixed_report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (reports_dir / "comparison_report.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    _print_report(legacy_report.to_dict())
    _print_report(fixed_report.to_dict())
    print("== 对比 ==")
    for row in comparison["comparison"]["checks"]:
        print(
            f"  {row['id']:<36} legacy={row['legacy_status']:<13} fixed={row['fixed_status']:<13}"
            f" resolved={row['resolved']}"
        )
    print(f"旧版总体: {legacy_report.to_dict()['overall']['conclusion']}")
    print(f"修复版总体: {fixed_report.to_dict()['overall']['conclusion']}")
    print(f"报告已写入: {reports_dir}")
    return 0 if fixed_report.clean and not legacy_report.clean else 1


def _print_report(payload: dict) -> None:
    target = payload["target"]
    print(f"\n== 报告 {payload['label']} ({target['base_url']}) ==")
    for check in payload["checks"]:
        print(f"  [{check['status']:<13}] {check['id']:<36} {check['conclusion']}")
    summary = payload["summary"]
    print(
        f"  通过={summary['passed']} 漏洞={summary['vulnerabilities']} "
        f"未授权已拒绝={summary['unauthorized_rejected']} "
        f"未授权未拒绝={summary['unauthorized_not_rejected']}"
    )
    print(f"  总体: {payload['overall']['conclusion']}")


if __name__ == "__main__":
    sys.exit(main())
