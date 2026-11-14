"""自检：对生成的视频做「抽帧回读」，把帧内字幕与规划逐帧比对。

为什么要回读：需求要求「可逐帧核对」，只相信内存里的绘制结果等于自证。
这里用 ffmpeg 只解码抽样帧，按帧号重绘期望画面并逐像素比较；
同时核对解码出的总帧数与规划一致。编码是 FFV1 无损，比较无容差。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import FrameSpec
from .renderer import Renderer
from .video import count_frames, decode_selected

SAMPLE_FRAME_CAP = 40


def sample_indices(count: int, wanted: set[int]) -> list[int]:
    """抽样帧号：始终包含切点/空档边界帧，再均匀补足到上限。"""
    if count <= 0:
        return []
    picks = {i for i in wanted if 0 <= i < count}
    if count <= SAMPLE_FRAME_CAP:
        picks.update(range(count))
    else:
        step = max(1, count // SAMPLE_FRAME_CAP)
        picks.update(range(0, count, step))
        picks.add(count - 1)
    return sorted(picks)


def verify_video(
    *,
    ffmpeg: str,
    video_path: Path,
    frames: list[FrameSpec],
    renderer: Renderer,
    total_ms: int,
    boundary_frames: dict[str, list[int]],
) -> dict[str, Any]:
    """返回可写入报告的核对结果。"""
    boundary = sorted({i for pair in boundary_frames.values() for i in pair})
    samples = sample_indices(len(frames), set(boundary))
    decoded = decode_selected(ffmpeg, video_path, samples, renderer.width, renderer.height)
    decoded_count = count_frames(ffmpeg, video_path)

    mismatched: list[dict[str, Any]] = []
    missing: list[int] = []
    for index in samples:
        spec = frames[index]
        actual = decoded.get(index)
        if actual is None:
            missing.append(index)
            continue
        expected = renderer.render(spec, total_ms)
        if actual.size != expected.size:
            mismatched.append(
                {
                    "index": index,
                    "issue": "size_mismatch",
                    "expected": list(expected.size),
                    "actual": list(actual.size),
                }
            )
        elif actual.tobytes() != expected.tobytes():
            mismatched.append({"index": index, "issue": "pixel_mismatch"})

    ok = not mismatched and not missing and decoded_count == len(frames)
    return {
        "decoded_frame_count": decoded_count,
        "expected_frame_count": len(frames),
        "sampled_frames": samples,
        "sampled_count": len(samples),
        "boundary_frames": boundary_frames,
        "ok": ok,
        "pixel_identical": not mismatched,
        "missing_frames": missing,
        "mismatches": mismatched,
        "note": "FFV1 无损编码；抽帧解码后与内存重绘逐像素比较，无容差",
    }
