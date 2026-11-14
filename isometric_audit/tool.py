"""命令行工具：读取轨道与遮挡表，逐帧输出可见瓦片与失效遮挡对。

用法:
    python -m isometric_audit.tool \
        --track sample_track.json \
        --occlusion sample_occlusion.json \
        --output report.json

退出码:
    0  成功
    1  数据校验失败（错误信息含字段名），不产出报告
    2  输入路径不存在或 JSON 无法解析，不产出报告
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import List, Optional, Sequence, Tuple

from .errors import OcclusionValidationError, TrackValidationError
from . import geometry

# 报告 JSON 顶层字段顺序仅作展示；实际写入开启 sort_keys，确定性不依赖插入顺序。


def _is_positive_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and value > 0
    )


def _require_positive(container: dict, field: str, path: str) -> float:
    """从 container 取字段，缺失或非正数时抛出含字段路径的错误。"""
    if field not in container:
        raise TrackValidationError(f"{path}.{field}: 字段缺失")
    value = container[field]
    if not _is_positive_number(value):
        raise TrackValidationError(
            f"{path}.{field}: 必须为正数，实际值为 {value!r}"
        )
    return float(value)


def validate_track(track: dict) -> dict:
    """校验轨道并返回归一化后的尺寸参数。任何失败都在写报告之前抛出。"""
    if not isinstance(track, dict):
        raise TrackValidationError("track: 根节点必须为 JSON 对象")

    map_obj = track.get("map")
    if not isinstance(map_obj, dict):
        raise TrackValidationError("track.map: 字段缺失或不是对象")
    cols = _require_positive(map_obj, "cols", "track.map")
    rows = _require_positive(map_obj, "rows", "track.map")

    tile_obj = track.get("tile_size")
    if not isinstance(tile_obj, dict):
        raise TrackValidationError("track.tile_size: 字段缺失或不是对象")
    tile_w = _require_positive(tile_obj, "w", "track.tile_size")
    tile_h = _require_positive(tile_obj, "h", "track.tile_size")

    vp_obj = track.get("viewport")
    if not isinstance(vp_obj, dict):
        raise TrackValidationError("track.viewport: 字段缺失或不是对象")
    vp_w = _require_positive(vp_obj, "w", "track.viewport")
    vp_h = _require_positive(vp_obj, "h", "track.viewport")

    keyframes = track.get("keyframes")
    if not isinstance(keyframes, list) or not keyframes:
        raise TrackValidationError("track.keyframes: 必须为非空数组")
    for i, kf in enumerate(keyframes):
        if not isinstance(kf, dict):
            raise TrackValidationError(f"track.keyframes[{i}]: 必须为对象")
        for axis in ("x", "y"):
            if axis not in kf:
                raise TrackValidationError(
                    f"track.keyframes[{i}].{axis}: 字段缺失"
                )
            value = kf[axis]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
            ):
                raise TrackValidationError(
                    f"track.keyframes[{i}].{axis}: 必须为有限数值，实际值为 {value!r}"
                )
        for axis in ("viewport_w", "viewport_h"):
            if axis in kf and not _is_positive_number(kf[axis]):
                raise TrackValidationError(
                    f"track.keyframes[{i}].{axis}: 必须为正数，实际值为 {kf[axis]!r}"
                )

    steps = track.get("steps_per_gap", 1)
    if not isinstance(steps, int) or isinstance(steps, bool) or steps < 1:
        raise TrackValidationError(
            f"track.steps_per_gap: 必须为不小于 1 的整数，实际值为 {steps!r}"
        )

    return {
        "cols": int(cols),
        "rows": int(rows),
        "tile_w": tile_w,
        "tile_h": tile_h,
        "viewport_w": vp_w,
        "viewport_h": vp_h,
        "steps_per_gap": steps,
    }


def load_relations(
    occlusion: dict, cols: int, rows: int
) -> Tuple[List[dict], List[dict], List[dict]]:
    """清洗遮挡表。

    返回 (valid_relations, invalid_relations, ignored_duplicates):
    - 越界引用进入 invalid_relations，其余帧继续处理；
    - 重复编号保留首个，后续同编号进入 ignored_duplicates 并在报告注明。
    """
    if not isinstance(occlusion, dict) or not isinstance(
        occlusion.get("relations"), list
    ):
        raise OcclusionValidationError(
            "occlusion.relations: 字段缺失或不是数组"
        )

    max_id = cols * rows - 1
    valid: List[dict] = []
    invalid: List[dict] = []
    duplicates: List[dict] = []
    seen_ids = set()

    for i, rel in enumerate(occlusion["relations"]):
        if not isinstance(rel, dict):
            raise OcclusionValidationError(
                f"occlusion.relations[{i}]: 必须为对象"
            )
        rel_id = rel.get("id")
        if rel_id is None:
            raise OcclusionValidationError(
                f"occlusion.relations[{i}].id: 字段缺失"
            )
        for side in ("first", "second"):
            if side not in rel:
                raise OcclusionValidationError(
                    f"occlusion.relations[{i}].{side}: 字段缺失（关系 {rel_id}）"
                )
            value = rel[side]
            if not isinstance(value, int) or isinstance(value, bool):
                raise OcclusionValidationError(
                    f"occlusion.relations[{i}].{side}: 必须为整数编号，"
                    f"实际值为 {value!r}（关系 {rel_id}）"
                )

        entry = {"id": rel_id, "first": rel["first"], "second": rel["second"]}

        if rel_id in seen_ids:
            duplicates.append(dict(entry))
            continue

        out_of_range = [
            t for t in (entry["first"], entry["second"]) if not (0 <= t <= max_id)
        ]
        if out_of_range:
            invalid.append(
                {
                    **entry,
                    "out_of_range_tiles": out_of_range,
                    "valid_tile_range": [0, max_id],
                    "reason": "瓦片编号超出地图范围",
                }
            )
            seen_ids.add(rel_id)
            continue

        seen_ids.add(rel_id)
        valid.append(entry)

    return valid, invalid, duplicates


def _frame_labels(keyframes: List[dict], steps: int) -> List[dict]:
    """与 geometry.expanded_frames 等长的逐帧标注。"""
    labels = [{"kind": "keyframe", "keyframe_id": keyframes[0]["id"]}]
    for i in range(1, len(keyframes)):
        prev_id, cur_id = keyframes[i - 1]["id"], keyframes[i]["id"]
        for s in range(1, steps + 1):
            labels.append(
                {
                    "kind": "interpolated",
                    "between": [prev_id, cur_id],
                    "step": s,
                    "steps_per_gap": steps,
                }
            )
        labels.append({"kind": "keyframe", "keyframe_id": cur_id})
    return labels


def build_report(track: dict, occlusion: dict) -> dict:
    """构造完整报告对象（在此函数返回前不写任何输出文件）。"""
    params = validate_track(track)
    cols, rows = params["cols"], params["rows"]
    tile_w, tile_h = params["tile_w"], params["tile_h"]
    steps = params["steps_per_gap"]
    keyframes = track["keyframes"]

    valid_rels, invalid_rels, duplicates = load_relations(occlusion, cols, rows)

    cameras = geometry.expanded_frames(track, steps)
    labels = _frame_labels(keyframes, steps)

    frames_out = []
    total_violations = 0
    for frame_index, ((cam_x, cam_y, vp_w, vp_h), label) in enumerate(
        zip(cameras, labels)
    ):
        visible = geometry.visible_tiles(
            cols, rows, tile_w, tile_h, cam_x, cam_y, vp_w, vp_h
        )
        visible_set = set(visible)

        violated = []
        for rel in valid_rels:
            first, second = rel["first"], rel["second"]
            if first in visible_set and second in visible_set:
                if geometry.required_draw_order(first, second, cols) == 1:
                    violated.append(
                        {"id": rel["id"], "first": first, "second": second}
                    )
        total_violations += len(violated)

        frame_entry = {
            "frame": frame_index,
            **label,
            "camera": {"x": cam_x, "y": cam_y},
            "viewport": {"w": vp_w, "h": vp_h},
            "visible_tiles": visible,
            "violated_relation_ids": [v["id"] for v in violated],
            "violated_relations": violated,
        }
        frames_out.append(frame_entry)

    report = {
        "meta": {
            "map": {"cols": cols, "rows": rows},
            "tile_size": {"w": tile_w, "h": tile_h},
            "keyframe_count": len(keyframes),
            "steps_per_gap": steps,
        },
        "invalid_relations": invalid_rels,
        "ignored_duplicates": duplicates,
        "frames": frames_out,
        "summary": {
            "frame_count": len(frames_out),
            "total_violations": total_violations,
            "invalid_relation_count": len(invalid_rels),
            "ignored_duplicate_count": len(duplicates),
        },
    }
    return report


def write_report(report: dict, output_path: str) -> None:
    """确定性写盘：UTF-8、LF 行尾、固定缩进与键排序，保证逐字节可复现。"""
    parent = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(parent, exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def _load_json(path: str, role: str) -> dict:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{role}路径不存在：{path}")
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{role}不是合法 JSON（{path}）：{exc}") from exc


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Windows 默认控制台可能为 GBK，统一为 UTF-8；失败时回退不影响主流程
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="等距瓦片相机轨道视口裁剪与遮挡核对工具"
    )
    parser.add_argument("--track", required=True, help="轨道 JSON 路径")
    parser.add_argument("--occlusion", required=True, help="遮挡表 JSON 路径")
    parser.add_argument("--output", required=True, help="输出报告 JSON 路径")
    args = parser.parse_args(argv)

    try:
        track = _load_json(args.track, "轨道文件")
        occlusion = _load_json(args.occlusion, "遮挡表文件")
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    try:
        report = build_report(track, occlusion)
    except (TrackValidationError, OcclusionValidationError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    write_report(report, args.output)

    summary = report["summary"]
    print(
        f"处理完成：帧数 {summary['frame_count']}，"
        f"失效遮挡对总数 {summary['total_violations']}，"
        f"无效关系 {summary['invalid_relation_count']}，"
        f"忽略重复 {summary['ignored_duplicate_count']}"
    )
    print(f"报告已写入：{args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
