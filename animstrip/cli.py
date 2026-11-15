"""命令行入口：python -m animstrip.cli <manifest.json> --output-dir <dir>"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pipeline import run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="按动画清单合成透明通道条带预览图并输出核对结果 JSON"
    )
    parser.add_argument("manifest", help="动画清单 JSON 路径")
    parser.add_argument(
        "-o",
        "--output-dir",
        default="strip_output",
        help="条带与 result.json 的输出目录（默认 strip_output）",
    )
    args = parser.parse_args(argv)

    report = run_pipeline(Path(args.manifest), Path(args.output_dir))
    passed = sum(1 for row in report["animations"] if row["passed"])
    print(
        f"共 {report['animation_count']} 个动画，"
        f"产出 {report['strip_count']} 张条带，通过 {passed} 个，"
        f"结果见 {Path(args.output_dir) / 'result.json'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
