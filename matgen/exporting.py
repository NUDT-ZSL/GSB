"""原子导出与旧版本归档。

输出目录按材质隔离；每次导出先写同目录临时文件再整体替换。
若已有输出来自不同版本标识，不直接覆盖，而是整体移动到归档目录。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


def _atomic_write_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


def material_output_dir(output_root: str | Path, material_id: str) -> Path:
    return Path(output_root) / material_id


def _archive_if_version_changed(out_dir: Path, archive_root: Path, material_id: str, version_id: str) -> dict | None:
    marker = out_dir / "version.json"
    if not marker.exists():
        return None
    try:
        old = json.loads(marker.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        old = {}
    if old.get("version_id") == version_id:
        return None

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive_dir = Path(archive_root) / material_id / f"{old.get('version_id', 'unknown')[:12]}_{stamp}"
    archive_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(out_dir), str(archive_dir))
    return {"archived_from": str(out_dir), "archived_to": str(archive_dir), "old_version_id": old.get("version_id")}


def export_material(
    output_root: str | Path,
    archive_root: str | Path,
    material_id: str,
    version_id: str,
    composite: dict[str, np.ndarray],
    tiles: list[dict],
    summary: dict,
    dtype=np.float32,
) -> dict:
    """原子导出整幅纹理、切片预览与报告，返回导出清单。"""
    out_dir = material_output_dir(output_root, material_id)
    archived = _archive_if_version_changed(out_dir, archive_root, material_id, version_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    arrays = {name: data.astype(dtype) for name, data in composite.items()}
    tmp_npz = out_dir / "composite.npz.tmp"
    with open(tmp_npz, "wb") as handle:
        np.savez_compressed(handle, **arrays)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_npz, out_dir / "composite.npz")

    preview_dir = out_dir / "tiles"
    preview_dir.mkdir(exist_ok=True)
    tile_index = []
    for tile in sorted(tiles, key=lambda t: t["index"]):
        tile_arrays = {"weights": tile["weights"].astype(dtype)}
        for name, data in tile["channels"].items():
            tile_arrays[name] = data.astype(dtype)
        tile_file = preview_dir / f"tile_{tile['index']:04d}.npz"
        tmp_tile = tile_file.with_name(tile_file.name + ".tmp")
        with open(tmp_tile, "wb") as handle:
            np.savez_compressed(handle, **tile_arrays)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_tile, tile_file)
        tile_index.append(
            {
                "index": tile["index"],
                "row": tile["row"],
                "col": tile["col"],
                "file": tile_file.name,
                "x0": tile["x0"],
                "y0": tile["y0"],
                "tile_w": tile["tile_w"],
                "tile_h": tile["tile_h"],
            }
        )

    _atomic_write_text(out_dir / "tiles_index.json", json.dumps(tile_index, ensure_ascii=False, indent=2))
    _atomic_write_text(out_dir / "report.json", json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    _atomic_write_text(
        out_dir / "version.json",
        json.dumps({"material_id": material_id, "version_id": version_id}, ensure_ascii=False, indent=2, sort_keys=True),
    )
    return {"output_dir": str(out_dir), "archived": archived, "files": ["composite.npz", "tiles_index.json", "report.json", "version.json"]}
