"""分块级原子持久化、版本标识与恢复。

每个分块以 ``.npz``（数据）+ ``.json``（摘要/版本）成对落盘，
统一“同目录临时文件 + os.replace 整体替换”，崩溃只可能留下 tmp 残留，
不会出现被误认为完成的半成品正式文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import numpy as np

from .model import GenConfig, MaterialSpec
from .tiling import ChunkPlan

CHUNK_FILE_VERSION = 1


def _canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_version_id(spec: MaterialSpec, config: GenConfig) -> str:
    """由材质参数与生成配置规范化摘要构成的版本标识。"""
    payload = {
        "chunk_file_version": CHUNK_FILE_VERSION,
        "material": spec.canonical_dict(),
        "config": config.canonical_dict(),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def build_chunk_digest(version_id: str, plan: ChunkPlan, channels: dict, weights: np.ndarray) -> str:
    """分块参数摘要 + 数据内容摘要，恢复时用于识别损坏块。"""
    hasher = hashlib.sha256()
    hasher.update(version_id.encode("utf-8"))
    hasher.update(
        _canonical_json(
            {
                "index": plan.index,
                "row": plan.row,
                "col": plan.col,
                "x0": plan.x0,
                "y0": plan.y0,
                "tile_w": plan.tile_w,
                "tile_h": plan.tile_h,
                "core_x": plan.core_x,
                "core_y": plan.core_y,
                "core_w": plan.core_w,
                "core_h": plan.core_h,
                "channels": sorted(channels.keys()),
            }
        ).encode("utf-8")
    )
    hasher.update(weights.astype(np.float64).tobytes())
    for name in sorted(channels.keys()):
        hasher.update(name.encode("utf-8"))
        hasher.update(channels[name].astype(np.float64).tobytes())
    return hasher.hexdigest()


def material_state_dir(state_root: str | Path, material_id: str) -> Path:
    return Path(state_root) / material_id


def _chunk_paths(state_dir: Path, index: int) -> tuple[Path, Path]:
    return state_dir / f"chunk_{index:04d}.npz", state_dir / f"chunk_{index:04d}.json"


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


def _atomic_write_text(target: Path, text: str) -> None:
    _atomic_write_bytes(target, text.encode("utf-8"))


def write_chunk(
    state_root: str | Path,
    material_id: str,
    version_id: str,
    plan: ChunkPlan,
    tile: dict,
) -> Path:
    """原子写入一个已完成分块，返回其状态目录。"""
    state_dir = material_state_dir(state_root, material_id)
    state_dir.mkdir(parents=True, exist_ok=True)
    digest = build_chunk_digest(version_id, plan, tile["channels"], tile["weights"])
    npz_path, json_path = _chunk_paths(state_dir, plan.index)

    arrays = {"weights": tile["weights"].astype(np.float64)}
    for name, data in tile["channels"].items():
        arrays[name] = data.astype(np.float64)
    tmp_npz = npz_path.with_name(npz_path.name + ".tmp")
    with open(tmp_npz, "wb") as handle:
        np.savez_compressed(handle, **arrays)
        handle.flush()
        os.fsync(handle.fileno())

    meta = {
        "chunk_file_version": CHUNK_FILE_VERSION,
        "material_id": material_id,
        "version_id": version_id,
        "digest": digest,
        "index": plan.index,
        "row": plan.row,
        "col": plan.col,
        "x0": plan.x0,
        "y0": plan.y0,
        "tile_w": plan.tile_w,
        "tile_h": plan.tile_h,
        "core_x": plan.core_x,
        "core_y": plan.core_y,
        "core_w": plan.core_w,
        "core_h": plan.core_h,
    }
    # 先替换数据文件，再替换摘要文件；摘要存在即代表该块完整提交。
    os.replace(tmp_npz, npz_path)
    _atomic_write_text(json_path, json.dumps(meta, sort_keys=True, ensure_ascii=False, indent=2))
    return state_dir


def load_valid_chunk(state_root: str | Path, material_id: str, index: int, expected_version: str):
    """读取并校验已持久化分块。

    返回 tile 字典；版本不符、摘要损坏、文件缺失等均返回 (None, 业务码)。
    """
    from .errors import ErrorCode

    state_dir = material_state_dir(state_root, material_id)
    npz_path, json_path = _chunk_paths(state_dir, index)
    if not npz_path.exists() or not json_path.exists():
        return None, ErrorCode.E_CHUNK_DATA_MISSING

    try:
        meta = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, ErrorCode.E_CHUNK_DIGEST_MISMATCH

    if meta.get("version_id") != expected_version:
        return None, ErrorCode.E_CHUNK_DIGEST_MISMATCH

    try:
        with np.load(npz_path) as bundle:
            arrays = {name: bundle[name] for name in bundle.files}
    except (OSError, ValueError, KeyError):
        return None, ErrorCode.E_CHUNK_DIGEST_MISMATCH

    if "weights" not in arrays:
        return None, ErrorCode.E_CHUNK_DIGEST_MISMATCH
    weights = arrays.pop("weights")
    plan = ChunkPlan(
        index=meta["index"],
        row=meta["row"],
        col=meta["col"],
        core_x=meta["core_x"],
        core_y=meta["core_y"],
        core_w=meta["core_w"],
        core_h=meta["core_h"],
        x0=meta["x0"],
        y0=meta["y0"],
        tile_w=meta["tile_w"],
        tile_h=meta["tile_h"],
    )
    actual = build_chunk_digest(expected_version, plan, arrays, weights)
    if actual != meta.get("digest"):
        return None, ErrorCode.E_CHUNK_DIGEST_MISMATCH

    tile = dict(meta)
    tile["weights"] = weights
    tile["channels"] = arrays
    return tile, None


def list_completed_indices(state_root: str | Path, material_id: str, expected_version: str):
    """扫描状态目录，返回 (有效块索引列表, 损坏块索引->业务码)。"""
    state_dir = material_state_dir(state_root, material_id)
    valid: list[int] = []
    corrupt: dict[int, str] = {}
    if not state_dir.exists():
        return valid, corrupt
    for json_path in sorted(state_dir.glob("chunk_*.json")):
        try:
            index = int(json_path.stem.split("_")[1])
        except (IndexError, ValueError):
            continue
        _, code = load_valid_chunk(state_root, material_id, index, expected_version)
        if code is None:
            valid.append(index)
        else:
            corrupt[index] = code
    return sorted(valid), corrupt


def reset_material_state(state_root: str | Path, material_id: str) -> None:
    """版本不一致时整体重做：清空该材质全部状态，杜绝新旧切片混拼。"""
    state_dir = material_state_dir(state_root, material_id)
    if state_dir.exists():
        shutil.rmtree(state_dir)


def cleanup_tmp_files(state_root: str | Path) -> None:
    """强制终止后清理未完成的临时文件，正式文件不受影响。"""
    root = Path(state_root)
    if not root.exists():
        return
    for tmp in root.rglob("*.tmp"):
        try:
            tmp.unlink()
        except OSError:
            pass


def write_interrupt_marker(state_root: str | Path, material_id: str, next_index: int, version_id: str) -> Path:
    state_dir = material_state_dir(state_root, material_id)
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / "interrupted.json"
    payload = {"material_id": material_id, "next_index": next_index, "version_id": version_id}
    _atomic_write_text(marker, json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2))
    return marker


def clear_interrupt_marker(state_root: str | Path, material_id: str) -> None:
    marker = material_state_dir(state_root, material_id) / "interrupted.json"
    if marker.exists():
        marker.unlink()
