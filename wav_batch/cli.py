# -*- coding: utf-8 -*-
"""命令行入口：按 CSV 清单批量裁剪 WAV 并做 RMS 增益对齐。

用法示例：
    python -m wav_batch.cli --manifest samples/manifest.csv \
        --output-dir demo_output/wav --report demo_output/report.json
"""
from __future__ import annotations

import argparse
import os
import sys

# -I 隔离模式下部分 Python 构建不会把脚本目录加入 sys.path，
# 这里显式加入以保证同目录模块可被导入（不引入任何第三方路径）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 中文 Windows 控制台可能默认 GBK，统一以 UTF-8 输出中文报告信息
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from processor import process_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wav_batch",
        description="游戏本地化台词 WAV 批量处理工具：按清单裁剪时间区间、按目标 RMS 做增益对齐，"
        "输出新 WAV 并生成 JSON 报告（仅支持 16 位 PCM 双声道、8000~48000Hz WAV，无需第三方库）。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-m", "--manifest", required=True,
        help="清单 CSV 路径，列顺序固定为 id,source,start,end,target_dbfs,output；"
             "时间以秒为单位（支持三位小数），相对路径相对清单所在目录解析。",
    )
    parser.add_argument(
        "-o", "--output-dir", required=True,
        help="WAV 输出目录；清单 output 列为相对路径时相对该目录解析，目录不存在会自动创建。",
    )
    parser.add_argument(
        "-r", "--report", required=True,
        help="JSON 报告输出路径，报告按清单行顺序记录每条的状态、输出路径、错误信息与警告。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    report, code = process_manifest(args.manifest, args.output_dir, args.report)

    print("全局状态：%s" % report["global_status"])
    print("条目统计：共 %d 条，成功 %d 条，失败 %d 条"
          % (report["total"], report["succeeded"], report["failed"]))
    if report.get("error"):
        print("全局错误：%s" % report["error"])
    for e in report["entries"]:
        if e["status"] == "成功":
            extra = "（警告：%s）" % "；".join(e["warnings"]) if e["warnings"] else ""
            print("  [成功] %s -> %s%s" % (e["id"], e["output_path"], extra))
        else:
            print("  [失败] %s：%s" % (e["id"], e["error"]))
    print("报告已写入：%s" % args.report)
    return code


if __name__ == "__main__":
    sys.exit(main())
