# -*- coding: utf-8 -*-
"""样例报告对比工具：把实际报告与预期摘要逐项比对（不依赖服务，可独立运行）。

用法：
  python tests/compare_sample.py <实际报告.json> <预期摘要.json>
预期摘要结构见 samples/expected/expected_summary.json。
"""

from __future__ import annotations

import json
import sys

GROUPS = ("成功", "失败", "不可用")


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def compare(report: dict, expected: dict) -> list[str]:
    """返回问题列表；空列表表示完全一致。"""
    problems: list[str] = []

    # 1) 汇总计数
    if report.get("汇总") != expected.get("汇总"):
        problems.append(f"汇总不一致: 实际={report.get('汇总')} "
                        f"预期={expected.get('汇总')}")

    # 2) 每组行号（隐含组内升序与组间不交叉）
    for group in GROUPS:
        actual_lines = [e["行号"] for e in report.get(group, [])]
        expected_lines = expected.get("分组行号", {}).get(group, [])
        if actual_lines != expected_lines:
            problems.append(f"{group}组行号不一致: 实际={actual_lines} "
                            f"预期={expected_lines}")
        if actual_lines != sorted(actual_lines):
            problems.append(f"{group}组未按行号升序: {actual_lines}")

    # 3) 每行状态与最终状态
    expected_line_status = expected.get("行状态", {})
    expected_final = expected.get("行最终状态", {})
    actual_flat: dict[int, dict] = {}
    for group in GROUPS:
        for entry in report.get(group, []):
            actual_flat[entry["行号"]] = entry
    for line_no_text, status in expected_line_status.items():
        line_no = int(line_no_text)
        entry = actual_flat.get(line_no)
        if entry is None:
            problems.append(f"行 {line_no} 在报告中缺失")
            continue
        if entry.get("状态") != status:
            problems.append(f"行 {line_no} 状态不一致: 实际={entry.get('状态')} "
                            f"预期={status}")
    if len(actual_flat) != len(expected_line_status):
        problems.append(
            f"条目数不一致: 实际={len(actual_flat)} 预期={len(expected_line_status)}")
    for line_no_text, final_status in expected_final.items():
        entry = actual_flat.get(int(line_no_text))
        if entry and entry.get("最终状态") != final_status:
            problems.append(
                f"行 {line_no_text} 最终状态不一致: 实际={entry.get('最终状态')} "
                f"预期={final_status}")

    # 4) 每条必备三字段
    for line_no, entry in actual_flat.items():
        for key in ("行号", "状态", "消息"):
            if key not in entry:
                problems.append(f"行 {line_no} 缺少字段 {key}")
        if entry.get("状态") not in GROUPS:
            problems.append(f"行 {line_no} 状态取值非法: {entry.get('状态')}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("用法: python tests/compare_sample.py <实际报告.json> <预期摘要.json>",
              file=sys.stderr)
        return 2
    report = load_json(argv[1])
    expected = load_json(argv[2])
    problems = compare(report, expected)
    if problems:
        print("样例对比失败：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("样例对比通过：报告与预期摘要完全一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
