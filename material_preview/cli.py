"""命令行入口。

用法::

    python preview.py 场景JSON路径 [输出目录]

退出码
------
* 0：管线执行完成（允许存在被跳过/渲染失败的方向，详见核对 JSON）
* 2：场景配置非法或文件无法读取（整体拒绝，不产生任何 PNG/核对 JSON）
* 1：其他未预期错误
"""

from __future__ import annotations

import sys

from material_preview.errors import ConfigError
from material_preview.pipeline import REPORT_FILENAME, run_pipeline
from material_preview.scene import load_scene


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 1 or len(args) > 2:
        print("用法: python preview.py <场景JSON路径> [输出目录]", file=sys.stderr)
        return 1

    scene_path = args[0]
    output_dir = args[1] if len(args) == 2 else "output"

    try:
        scene = load_scene(scene_path)
    except ConfigError as exc:
        print(f"配置错误，拒绝执行：{exc}", file=sys.stderr)
        return 2

    try:
        report = run_pipeline(scene, output_dir)
    except ConfigError as exc:
        print(f"配置错误，拒绝执行：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"管线异常终止：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(
        "渲染完成：成功 {rendered} 个，跳过 {skipped} 个，失败 {failed} 个；"
        "核对报告：{report}".format(
            rendered=report["rendered_count"],
            skipped=report["skipped_count"],
            failed=report["failed_count"],
            report=f"{output_dir}/{REPORT_FILENAME}",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
