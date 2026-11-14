"""渲染管线：逐方向产出 PNG 并汇总核对 JSON。

设计要点
--------
* 输出目录不存在时自动创建（``parents=True``）。
* 每个光照方向独立 try/except：单个方向渲染失败不影响其他方向，
  失败项进入核对 JSON 的 ``failed`` 列表并附可读原因。
* 被跳过的零向量/非数值方向进入 ``skipped`` 列表；
  ``rendered`` 仅包含成功渲染项，按方向编号升序排列。
* JSON 以固定缩进、固定键序、UTF-8 无 BOM、``\\n`` 换行写出，逐次运行字节一致。
"""

from __future__ import annotations

import json
from pathlib import Path

from .render import render_direction, save_png
from .scene import Scene

REPORT_FILENAME = "check_report.json"


def _png_filename(light_index: int) -> str:
    # 两位编号在 MAX_LIGHT_DIRECTIONS=16 内不产生重名；固定宽度保证字典序与编号序一致。
    return f"light_{light_index:02d}.png"


def run_pipeline(scene: Scene, output_dir: str) -> dict:
    """执行完整渲染管线，返回写出的核对报告字典。"""
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    rendered: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []

    for light in scene.lights:
        if not light.valid:
            skipped.append({
                "index": light.index,
                "reason": light.skip_reason,
            })
            continue

        filename = _png_filename(light.index)
        try:
            result = render_direction(scene, light.direction)  # type: ignore[arg-type]
            save_png(result.image, str(out_path / filename))
        except Exception as exc:  # 单方向隔离：失败方向不拖垮整体
            failed.append({
                "index": light.index,
                "filename": filename,
                "reason": f"渲染失败：{type(exc).__name__}: {exc}",
            })
            continue

        rendered.append({
            "index": light.index,
            "filename": filename,
            "visible_sphere_count": result.visible_sphere_count,
            "has_specular": scene.material.has_specular,
        })

    report = {
        "canvas": {"width": scene.width, "height": scene.height},
        "background": list(scene.background),
        "total_directions": len(scene.lights),
        "rendered_count": len(rendered),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "rendered": rendered,
        "skipped": skipped,
        "failed": failed,
    }

    report_path = out_path / REPORT_FILENAME
    report_text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False)
    report_path.write_text(report_text + "\n", encoding="utf-8", newline="\n")
    return report
