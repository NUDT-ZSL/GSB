"""Tkinter 工作台启动入口（Windows 本地路径运行）。

示例:
    python run_workbench.py --manifest samples\\manifest.json
"""

import argparse
import sys

from gpu_diag.ui import launch, require_tk


def main(argv=None):
    parser = argparse.ArgumentParser(description="GPU 调度与配额诊断工作台")
    parser.add_argument("--manifest", default="samples/manifest.json",
                        help="本地清单 JSON 路径")
    parser.add_argument("--snapshot-dir", default="snapshots",
                        help="快照目录（Windows 本地路径）")
    args = parser.parse_args(argv)
    try:
        require_tk()
    except RuntimeError as exc:
        print("GUI 不可用: {}".format(exc))
        print("无图形环境可改用演示脚本: python scripts\\demo.py")
        return 2
    launch(args.manifest, args.snapshot_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
