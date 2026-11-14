"""命令行入口：``python -m material_preview``。

退出码约定：

* 0 —— 运行完成（即使部分条目未完成；未完成情况写在报告里）
* 1 —— 清单语法/结构错误、输出目录不安全
* 2 —— 未预期异常（不吞掉堆栈，便于排查）
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from .manifest import ManifestError
from .runner import UsageError, purge_after_manifest_error, run

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CRASH = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m material_preview",
        description="按清单逐项渲染材质在各水平光照方向下的预览。",
    )
    parser.add_argument("manifest", type=Path, help="清单 JSON 路径")
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=Path("out"),
        help="输出目录（默认 ./out；每次运行会先清掉其中上次留下的本程序产物）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run(args.manifest, args.out)
    except ManifestError as exc:
        # 清单不可用时不允许输出目录里残留上一次的预览（规则 #10）。
        purge_after_manifest_error(args.out, args.manifest)
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except UsageError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception:
        traceback.print_exc()
        return EXIT_CRASH

    summary = report["summary"]
    print(
        f"完成：{summary['completed']}/{summary['entries']} 个方向已渲染，"
        f"{summary['incomplete']} 个未完成；报告：{(args.out / 'report.json')}"
    )
    for entry in report["entries"]:
        if entry["status"] != "completed":
            print(
                f"  未完成：{entry['material']} @ {entry['angle']!r} — {entry['reason']}",
                file=sys.stderr,
            )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
