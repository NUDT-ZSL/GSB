# -*- coding: utf-8 -*-
"""命令行入口：编排 CSV 读取 -> 表单提交 -> 状态回读 -> JSON 报告。

用法（Windows 路径示例）：
  python -m employee_importer.cli \
      --input "E:\\Company\\demo\\projects\\project67\\samples\\data\\employees.csv" \
      --report "E:\\Company\\demo\\projects\\project67\\samples\\out\\report.json" \
      --url http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import os
import sys

from .browser_form import FormSession
from .csv_reader import read_employee_csv
from .reporter import (
    GROUPS,
    RowReport,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_UNAVAILABLE,
    build_report,
    write_report,
)
from .status_client import query_final_status


def _emit(line_no: int, status: str, message: str) -> None:
    """控制台逐行输出：行号 + 当前状态 + 消息（验收第 13 项）。"""
    print(f"行 {line_no} | {status} | {message}", flush=True)


def process_file(input_path: str, report_path: str, base_url: str,
                 headless: bool = True, browser: str = "chromium") -> dict:
    """处理整个 CSV 并写出报告，返回报告对象。"""
    rows = read_employee_csv(input_path)
    print(f"共读取 {len(rows)} 条有效数据行（源文件: {input_path}）", flush=True)

    reports: list[RowReport] = []
    with FormSession(base_url=base_url, headless=headless,
                     browser=browser) as session:
        for row in rows:
            print(f"行 {row.line_no}: 开始处理", flush=True)
            submit = session.submit_row(row.non_empty_fields())

            if submit.outcome == "failed":
                # 页面加载失败 / 元素缺失 / 无记录标识等：不触发回读
                item = RowReport(row.line_no, STATUS_FAILED, submit.message,
                                 attempts=submit.attempts)
                reports.append(item)
                _emit(row.line_no, STATUS_FAILED, submit.message)
                continue

            if submit.outcome == "unavailable":
                # 提交接口持续不可用：进入需跟进区，不触发回读
                item = RowReport(row.line_no, STATUS_UNAVAILABLE, submit.message,
                                 attempts=submit.attempts)
                reports.append(item)
                _emit(row.line_no, STATUS_UNAVAILABLE, submit.message)
                continue

            # submit.outcome == "submitted"：凭记录标识回读最终状态
            record_id = submit.record_id
            status = query_final_status(base_url, record_id)
            if status.status == "ok":
                message = (f"记录 {record_id} 回读成功；{status.message}"
                           f"（提交尝试 {submit.attempts} 次，"
                           f"状态查询 {status.attempts} 次）")
                item = RowReport(row.line_no, STATUS_SUCCESS, message,
                                 record_id=record_id,
                                 final_status=status.final_status,
                                 attempts=submit.attempts + status.attempts)
                reports.append(item)
                _emit(row.line_no, STATUS_SUCCESS, message)
            else:
                message = (f"记录 {record_id} 已提交但回读失败：{status.message}"
                           f"（状态查询 {status.attempts} 次），列入需跟进")
                item = RowReport(row.line_no, STATUS_UNAVAILABLE, message,
                                 record_id=record_id, attempts=status.attempts)
                reports.append(item)
                _emit(row.line_no, STATUS_UNAVAILABLE, message)

    report = build_report(input_path, reports)
    parent = os.path.dirname(os.path.abspath(report_path))
    os.makedirs(parent, exist_ok=True)
    write_report(report_path, report)

    summary = report["汇总"]
    print("-" * 60, flush=True)
    print(f"报告已写出: {report_path}", flush=True)
    print(
        "汇总: 总 {总条数} 条 | 成功 {成功} | 失败 {失败} | 不可用 {不可用}".format(
            **summary),
        flush=True,
    )
    # 组内排序在 build_report 中完成，这里再输出每组行号便于现场核对
    for group in GROUPS:
        line_nos = [entry["行号"] for entry in report[group]]
        print(f"{group}组行号: {line_nos}", flush=True)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="员工信息录入：读取 CSV，经本地 Web 表单逐条提交并回读状态，输出 JSON 报告")
    parser.add_argument("--input", "-i", required=True,
                        help="输入 CSV 路径（Windows 写法，如 E:\\\\data\\\\employees.csv）")
    parser.add_argument("--report", "-r", required=True,
                        help="报告 JSON 输出路径（Windows 写法）")
    parser.add_argument("--url", default="http://127.0.0.1:8765",
                        help="本地模拟表单服务根地址（默认 http://127.0.0.1:8765）")
    parser.add_argument("--browser", default="chromium",
                        choices=("chromium", "chrome", "msedge"),
                        help="浏览器选择：chromium(默认，缺失时自动回退系统 Chrome/Edge)")
    parser.add_argument("--no-headless", action="store_true",
                        help="显示浏览器窗口（默认无头运行）")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 验收第 12 项：输入文件不存在或无法读取时，报错退出且不生成报告文件
    if not os.path.isfile(args.input):
        print(f"错误：输入文件不存在或不是文件: {args.input}", file=sys.stderr)
        return 2
    try:
        with open(args.input, "r", encoding="utf-8-sig", newline=""):
            pass
    except OSError as exc:
        print(f"错误：输入文件无法读取: {args.input}（{exc}）", file=sys.stderr)
        return 2

    try:
        process_file(
            input_path=args.input,
            report_path=args.report,
            base_url=args.url,
            headless=not args.no_headless,
            browser=args.browser,
        )
    except Exception as exc:  # 运行期致命错误：明确报错，不留半成品报告
        print(f"错误：处理过程中发生致命异常: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
