"""动画清单 JSON 的加载与结构校验。

校验规则（不通过的条目不进入合成，但仍写入核对结果）：
- animation_id 必须是非空字符串且不含路径分隔符；同清单内不得重复
- target_duration_ms 必须为正数
- frames 必须为非空列表，每帧 index 为整数且不重复，duration_ms 为非负整数，
  image 为非空字符串
- 动画总数 <= 40、每动画帧数 <= 60
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import AnimationSpec, FrameSpec, ParsedAnimation

MAX_ANIMATIONS = 40
MAX_FRAMES_PER_ANIMATION = 60


def load_manifest(manifest_path: Path) -> list[ParsedAnimation]:
    """加载并校验清单文件，返回每个动画条目的解析结果。"""
    manifest_path = Path(manifest_path)
    with manifest_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, dict) or not isinstance(data.get("animations"), list):
        raise ValueError("清单格式错误：顶层需为对象且包含 animations 数组")

    entries = data["animations"]
    if len(entries) > MAX_ANIMATIONS:
        raise ValueError(
            f"清单动画数 {len(entries)} 超过上限 {MAX_ANIMATIONS}"
        )

    manifest_dir = manifest_path.parent.resolve()
    parsed: list[ParsedAnimation] = []
    seen_ids: set[str] = set()

    for pos, entry in enumerate(entries):
        anim_id, issues = _validate_entry(entry, seen_ids)
        if anim_id is not None and anim_id not in seen_ids:
            seen_ids.add(anim_id)

        spec: AnimationSpec | None = None
        target = entry.get("target_duration_ms") if isinstance(entry, dict) else None
        if not issues:
            frames = tuple(
                FrameSpec(
                    index=int(item["index"]),
                    image=str(item["image"]),
                    duration_ms=int(item["duration_ms"]),
                )
                for item in entry["frames"]
            )
            spec = AnimationSpec(
                animation_id=anim_id,
                target_duration_ms=int(entry["target_duration_ms"]),
                frames=frames,
                manifest_dir=manifest_dir,
            )
        parsed.append(
            ParsedAnimation(
                animation_id=anim_id if anim_id is not None else f"<entry#{pos}>",
                target_duration_ms=target if isinstance(target, int) else None,
                spec=spec,
                issues=issues,
            )
        )

    return parsed


def _validate_entry(
    entry: object, seen_ids: set[str]
) -> tuple[str | None, list[str]]:
    issues: list[str] = []
    if not isinstance(entry, dict):
        return None, ["清单条目不是 JSON 对象"]

    anim_id = entry.get("animation_id")
    if not isinstance(anim_id, str) or not anim_id.strip():
        issues.append("animation_id 缺失或不是非空字符串")
        anim_id = None
    elif "/" in anim_id or "\\" in anim_id or anim_id in (".", ".."):
        issues.append(f"animation_id 非法（不得包含路径分隔符）：{anim_id}")
        anim_id = None
    elif anim_id in seen_ids:
        issues.append(f"animation_id 重复：{anim_id}")
        anim_id = None

    target = entry.get("target_duration_ms")
    if not isinstance(target, int) or isinstance(target, bool) or target <= 0:
        issues.append("target_duration_ms 必须为正数")

    frames = entry.get("frames")
    frame_specs: list[FrameSpec] = []
    if not isinstance(frames, list) or not frames:
        issues.append("frames 缺失或为空数组")
    else:
        if len(frames) > MAX_FRAMES_PER_ANIMATION:
            issues.append(
                f"帧数 {len(frames)} 超过上限 {MAX_FRAMES_PER_ANIMATION}"
            )
        seen_indexes: set[int] = set()
        for pos, item in enumerate(frames):
            if not isinstance(item, dict):
                issues.append(f"第 {pos} 帧不是 JSON 对象")
                continue
            idx = item.get("index")
            if not isinstance(idx, int) or isinstance(idx, bool):
                issues.append(f"第 {pos} 帧 index 缺失或不是整数")
            elif idx in seen_indexes:
                issues.append(f"帧序号重复：{idx}")
            else:
                seen_indexes.add(idx)

            image = item.get("image")
            if not isinstance(image, str) or not image.strip():
                issues.append(f"第 {pos} 帧 image 缺失或不是非空字符串")

            duration = item.get("duration_ms")
            if (
                not isinstance(duration, int)
                or isinstance(duration, bool)
                or duration < 0
            ):
                issues.append(f"第 {pos} 帧 duration_ms 缺失或不是非负整数")
            elif isinstance(idx, int) and isinstance(image, str) and image.strip():
                frame_specs.append(FrameSpec(idx, image, duration))

    if issues:
        return anim_id, issues
    return anim_id, []
