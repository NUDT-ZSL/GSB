"""确定性样例生成器：``python -m material_preview.samples <场景> <目录>``。

不传场景时列出全部可选场景。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .samples import BUILDERS, build


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m material_preview.samples",
        description="生成确定性的清单 JSON 与贴图 PNG 样例。",
    )
    parser.add_argument("scenario", nargs="?", help=f"样例名：{', '.join(sorted(BUILDERS))}")
    parser.add_argument("root", nargs="?", type=Path, help="输出目录（必须不存在或为空）")
    args = parser.parse_args(argv)

    if not args.scenario:
        for name in sorted(BUILDERS):
            print(name)
        return 0
    if args.root is None:
        parser.error("需要同时给出场景名和输出目录")
    manifest = build(args.scenario, args.root.resolve())
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
