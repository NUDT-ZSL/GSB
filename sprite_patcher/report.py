"""逐格差异比较与差异报告 JSON 生成。

排序规则：变化像素数降序；并列时按格子序号（行优先）升序，保证相同输入
重复运行时报告内容与排序完全一致。报告不含时间戳等非确定字段。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from PIL import Image

from .config import Config
from .geometry import Geometry, divisible_hint
from .patcher import CellStat, PatchOutcome


@dataclass
class CellDiff:
    row: int
    column: int
    index: int
    changed_pixels: int
    total_pixels: int
    changed: bool


def diff_cells(
    old: Image.Image, new: Image.Image, config: Config, geometry: Geometry
) -> list[CellDiff]:
    if old.mode != "RGBA":
        old = old.convert("RGBA")
    if new.mode != "RGBA":
        new = new.convert("RGBA")
    old_px = old.load()
    new_px = new.load()
    results: list[CellDiff] = []

    for r in range(config.grid.rows):
        for c in range(config.grid.columns):
            left, top, right, bottom = geometry.cell_box(r, c)
            changed = 0
            total = 0
            for y in range(top, bottom):
                for x in range(left, right):
                    total += 1
                    if old_px[x, y] != new_px[x, y]:
                        changed += 1
            results.append(
                CellDiff(
                    row=r,
                    column=c,
                    index=r * config.grid.columns + c,
                    changed_pixels=changed,
                    total_pixels=total,
                    changed=changed > 0,
                )
            )

    # 变化数降序，并列时序号升序（稳定确定）
    results.sort(key=lambda d: (-d.changed_pixels, d.index))
    return results


def build_report(
    config: Config,
    geometry: Geometry,
    outcomes: list[PatchOutcome],
    cell_stats: dict[int, CellStat],
    diffs: list[CellDiff],
    input_sheet: str,
    output_sheet: str,
) -> dict:
    total_changed = sum(d.changed_pixels for d in diffs)
    applied = [o for o in outcomes if o.status == "applied"]
    unavailable = [o for o in outcomes if o.status == "unavailable"]
    no_substantial = len(applied) == 0 or total_changed == 0

    cell_records = []
    for d in diffs:
        stat = cell_stats.get(d.index)
        cell_records.append(
            {
                "index": d.index,
                "row": d.row,
                "column": d.column,
                "changed_pixels": d.changed_pixels,
                "total_pixels": d.total_pixels,
                "verdict": "已改动" if d.changed else "未改动",
                "covered_pixels": stat.covered_pixels if stat else 0,
                "retained_original_pixels": (
                    stat.retained_original_pixels if stat else d.total_pixels
                ),
                "applied_patch_orders": stat.patch_orders if stat else [],
            }
        )

    return {
        "schema_version": 1,
        "input": {
            "sheet_path": input_sheet,
            "config_path": str(config.config_path),
        },
        "output_sheet_path": output_sheet,
        "sheet": {"width": config.sheet.width, "height": config.sheet.height},
        "grid": {"rows": config.grid.rows, "columns": config.grid.columns},
        "cell_size": {"width": geometry.cell_w, "height": geometry.cell_h},
        "edge_bands": {
            "right": _band_dict(geometry.right_band),
            "bottom": _band_dict(geometry.bottom_band),
            "divisibility_hint": divisible_hint(config.sheet, config.grid),
        },
        "patches": [asdict(o) for o in outcomes],
        "cells": cell_records,
        "summary": {
            "patch_count": len(outcomes),
            "applied_patch_count": len(applied),
            "unavailable_patch_count": len(unavailable),
            "changed_cell_count": sum(1 for d in diffs if d.changed),
            "total_changed_pixels": total_changed,
            "no_substantial_change": no_substantial,
            "note": (
                "本次无实质修改：没有有效补丁或全部格子变化为 0，"
                "新精灵表与原精灵表逐像素相同。"
                if no_substantial
                else "本次存在实质修改。"
            ),
        },
    }


def _band_dict(band) -> dict:
    return {"side": band.side, "position": band.position, "pixels": band.pixels}
