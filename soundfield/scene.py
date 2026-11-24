"""录制棚场景 JSON 的读取与全量输入校验。"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import errors as E
from .config import ALLOWED_LAYOUTS, ALLOWED_SAMPLE_RATES, WALL_IDS


def _is_int(v: Any) -> bool:
    # bool 是 int 的子类，显式排除。
    return isinstance(v, int) and not isinstance(v, bool)


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


@dataclass(frozen=True)
class Point:
    id: str
    position: tuple[float, float, float]
    wav: str | None = None


@dataclass(frozen=True)
class Scene:
    raw: dict[str, Any]
    path: str
    seed: int
    room_size: tuple[float, float, float]
    absorption_q: dict[str, tuple[int, ...]]          # wall -> 每频段 Q24 吸声
    absorption_f: dict[str, tuple[float, ...]]
    sample_rate: int
    layout: str
    reconstruction_order: int
    max_reflections: int
    band_edges_hz: tuple[float, ...]
    band_gains_f: tuple[float, ...]
    sources: tuple[Point, ...]
    speakers: tuple[Point, ...]
    max_workers: int
    memory_budget_bytes: int
    ir_length_samples: int

    @property
    def band_count(self) -> int:
        return len(self.band_edges_hz) + 1

    @property
    def nyquist_hz(self) -> float:
        return self.sample_rate / 2.0

    def canonical_json(self) -> bytes:
        """规范化场景摘要（版本标识的一部分），键排序、无空格。"""

        return json.dumps(self.raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )


def load_scene(scene_path: str) -> Scene:
    try:
        with open(scene_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise E.BusinessError(E.E_SCENE_IO, f"场景文件无法读取: {scene_path}", {"path": scene_path}) from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise E.BusinessError(
            E.E_JSON_PARSE,
            "场景 JSON 解析失败",
            {"path": scene_path, "line": exc.lineno, "column": exc.colno},
        ) from exc
    if not isinstance(raw, dict):
        raise E.BusinessError(E.E_SCENE_SCHEMA, "场景根节点必须是 JSON 对象")
    base_dir = os.path.dirname(os.path.abspath(scene_path))
    return _build_scene(raw, base_dir)


def _require(raw: dict[str, Any], key: str) -> Any:
    if key not in raw:
        raise E.BusinessError(E.E_SCENE_SCHEMA, f"缺少必填字段: {key}", {"field": key})
    return raw[key]


def _build_scene(raw: dict[str, Any], base_dir: str) -> Scene:
    seed = _require(raw, "seed")
    if not _is_int(seed) or seed < 0:
        raise E.BusinessError(E.E_SCENE_SCHEMA, "seed 必须是非负整数", {"field": "seed"})

    room = _require(raw, "room")
    if not isinstance(room, dict) or not isinstance(room.get("size"), list) or len(room["size"]) != 3:
        raise E.BusinessError(E.E_SCENE_SCHEMA, "room.size 必须是长度 3 的数组")
    size = room["size"]
    if not all(_finite(v) and float(v) > 0 for v in size):
        raise E.BusinessError(
            E.E_DIMENSION,
            "棚体三轴尺寸必须是有限正数",
            {"size": list(size)},
        )
    room_size = tuple(float(v) for v in size)

    sample_rate = _require(raw, "sample_rate")
    if not _is_int(sample_rate) or sample_rate not in ALLOWED_SAMPLE_RATES:
        raise E.BusinessError(
            E.E_SAMPLE_RATE,
            f"采样率不在允许集合 {ALLOWED_SAMPLE_RATES}",
            {"sample_rate": sample_rate},
        )

    layout = _require(raw, "layout")
    if layout not in ALLOWED_LAYOUTS:
        raise E.BusinessError(
            E.E_LAYOUT,
            f"声道布局不在允许集合 {sorted(ALLOWED_LAYOUTS)}",
            {"layout": layout},
        )

    reconstruction_order = _require(raw, "reconstruction_order")
    max_reflections = _require(raw, "max_reflections")
    if not _is_int(reconstruction_order) or reconstruction_order < 0:
        raise E.BusinessError(E.E_ORDER, "reconstruction_order 必须是非负整数")
    if not _is_int(max_reflections) or max_reflections < 0:
        raise E.BusinessError(E.E_ORDER, "max_reflections 必须是非负整数")
    if reconstruction_order > max_reflections:
        raise E.BusinessError(
            E.E_ORDER,
            "reconstruction_order 不得大于 max_reflections",
            {"reconstruction_order": reconstruction_order, "max_reflections": max_reflections},
        )

    edges = _require(raw, "band_edges_hz")
    if not isinstance(edges, list) or not edges:
        raise E.BusinessError(E.E_BAND, "band_edges_hz 必须是非空数组")
    if not all(_finite(v) and float(v) > 0 for v in edges):
        raise E.BusinessError(E.E_BAND, "频段边界必须是有限正数")
    edges_f = tuple(float(v) for v in edges)
    if any(edges_f[i] >= edges_f[i + 1] for i in range(len(edges_f) - 1)):
        raise E.BusinessError(E.E_BAND, "频段边界必须严格递增", {"edges": list(edges_f)})
    nyquist = sample_rate / 2.0
    if edges_f[-1] >= nyquist:
        raise E.BusinessError(
            E.E_BAND,
            "最高频段边界必须落在奈奎斯特频率内",
            {"max_edge": edges_f[-1], "nyquist": nyquist},
        )
    band_count = len(edges_f) + 1

    gains = raw.get("band_gains")
    if gains is None:
        band_gains = tuple(1.0 for _ in range(band_count))
    else:
        if not isinstance(gains, list) or len(gains) != band_count:
            raise E.BusinessError(
                E.E_BAND,
                "band_gains 长度必须等于频段数（边界数 + 1）",
                {"expected": band_count, "got": None if not isinstance(gains, list) else len(gains)},
            )
        if not all(_finite(v) and float(v) > 0 for v in gains):
            raise E.BusinessError(E.E_BAND, "频段增益必须是有限正数")
        band_gains = tuple(float(v) for v in gains)

    absorption_raw = _require(raw, "absorption")
    if not isinstance(absorption_raw, dict) or set(absorption_raw.keys()) != set(WALL_IDS):
        raise E.BusinessError(
            E.E_ABSORPTION,
            f"absorption 必须且只能包含六面墙 {WALL_IDS}",
            {"keys": sorted(absorption_raw.keys()) if isinstance(absorption_raw, dict) else None},
        )
    absorption_q: dict[str, tuple[int, ...]] = {}
    absorption_f: dict[str, tuple[float, ...]] = {}
    for wall in WALL_IDS:
        coeffs = absorption_raw[wall]
        if _finite(coeffs):
            coeffs = [coeffs] * band_count
        elif isinstance(coeffs, list) and len(coeffs) == band_count and all(_finite(v) for v in coeffs):
            coeffs = [float(v) for v in coeffs]
        else:
            raise E.BusinessError(
                E.E_ABSORPTION,
                f"墙面 {wall} 的吸声系数必须是数值或长度 {band_count} 的数值数组",
                {"wall": wall},
            )
        if any(not (0.0 < v < 1.0) for v in coeffs):
            raise E.BusinessError(
                E.E_ABSORPTION,
                f"墙面 {wall} 的吸声系数必须位于开区间 (0,1)",
                {"wall": wall, "coeffs": coeffs},
            )
        from .fixedpoint import quantize

        absorption_f[wall] = tuple(coeffs)
        absorption_q[wall] = tuple(quantize(v) for v in coeffs)

    sources = _parse_points(raw, "sources", room_size, base_dir, need_wav=True)
    speakers = _parse_points(raw, "speakers", room_size, base_dir, need_wav=False)
    if len(speakers) != ALLOWED_LAYOUTS[layout]:
        raise E.BusinessError(
            E.E_LAYOUT,
            "扬声器数量与声道布局不一致",
            {"layout": layout, "expected": ALLOWED_LAYOUTS[layout], "got": len(speakers)},
        )
    all_points = list(sources) + list(speakers)
    seen: dict[tuple[float, float, float], str] = {}
    for point in all_points:
        if point.position in seen:
            raise E.BusinessError(
                E.E_DUPLICATE_POINT,
                "声源/扬声器坐标互不重合",
                {"point": list(point.position), "ids": [seen[point.position], point.id]},
            )
        seen[point.position] = point.id

    max_workers = raw.get("max_workers", 1)
    if not _is_int(max_workers) or max_workers <= 0:
        raise E.BusinessError(E.E_SCENE_SCHEMA, "max_workers 必须是正整数")
    memory_budget = raw.get("memory_budget_bytes", 64 * 1024 * 1024)
    if not _is_int(memory_budget) or memory_budget <= 0:
        raise E.BusinessError(E.E_BUDGET, "memory_budget_bytes 必须是正整数")
    ir_length = raw.get("ir_length_samples", 0)
    if not _is_int(ir_length) or ir_length < 0:
        raise E.BusinessError(E.E_SCENE_SCHEMA, "ir_length_samples 必须是非负整数")

    return Scene(
        raw=raw,
        path=os.path.join(base_dir, ""),
        seed=int(seed),
        room_size=room_size,  # type: ignore[arg-type]
        absorption_q=absorption_q,
        absorption_f=absorption_f,
        sample_rate=int(sample_rate),
        layout=str(layout),
        reconstruction_order=int(reconstruction_order),
        max_reflections=int(max_reflections),
        band_edges_hz=edges_f,
        band_gains_f=band_gains,
        sources=sources,
        speakers=speakers,
        max_workers=int(max_workers),
        memory_budget_bytes=int(memory_budget),
        ir_length_samples=int(ir_length),
    )


def _parse_points(
    raw: dict[str, Any],
    key: str,
    room_size: tuple[float, float, float],
    base_dir: str,
    *,
    need_wav: bool,
) -> tuple[Point, ...]:
    items = _require(raw, key)
    if not isinstance(items, list) or not items:
        raise E.BusinessError(E.E_SCENE_SCHEMA, f"{key} 必须是非空数组")
    points: list[Point] = []
    ids: set[str] = set()
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise E.BusinessError(E.E_SCENE_SCHEMA, f"{key}[{idx}] 必须是对象")
        point_id = item.get("id")
        if not isinstance(point_id, str) or not point_id:
            raise E.BusinessError(E.E_SCENE_SCHEMA, f"{key}[{idx}].id 必须是非空字符串")
        if point_id in ids:
            raise E.BusinessError(E.E_DUPLICATE_POINT, f"{key} 内 id 重复: {point_id}")
        ids.add(point_id)
        pos = item.get("position")
        if not isinstance(pos, list) or len(pos) != 3 or not all(_finite(v) for v in pos):
            raise E.BusinessError(E.E_COORDINATE, f"{key}[{point_id}].position 必须是 3 个有限数")
        position = tuple(float(v) for v in pos)
        if any(not (0.0 < position[axis] < room_size[axis]) for axis in range(3)):
            raise E.BusinessError(
                E.E_COORDINATE,
                f"{key}[{point_id}] 坐标必须严格位于棚内 (0, 棚尺寸)",
                {"id": point_id, "position": list(position)},
            )
        wav = None
        if need_wav:
            wav_name = item.get("wav")
            if not isinstance(wav_name, str) or not wav_name:
                raise E.BusinessError(E.E_SCENE_SCHEMA, f"{key}[{point_id}].wav 必须是非空字符串")
            wav = wav_name if os.path.isabs(wav_name) else os.path.normpath(os.path.join(base_dir, wav_name))
        points.append(Point(id=point_id, position=position, wav=wav))  # type: ignore[arg-type]
    return tuple(points)


def room_half_extents(room_size: tuple[float, float, float]) -> np.ndarray:
    return np.asarray(room_size, dtype=np.float64) / 2.0
