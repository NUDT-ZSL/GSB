# -*- coding: utf-8 -*-
"""
shot_cover.py — 视频镜头封面候选帧提取工具

功能:
  1. 读取本地 H.264 MP4（其他 OpenCV 可解码格式亦可），解析帧序列与总时长；
  2. 基于帧间画面差异做硬切镜头检测，给出每个镜头的起止时间；
  3. 每个镜头在中部区域挑选一张非黑候选帧，输出 JPEG；
  4. 生成 JSON 报告（总时长、镜头数、候选帧文件名/毫秒时间戳/镜头序号）；
  5. 控制台打印检测镜头数与候选帧数摘要；
  6. 文件无法解码/不支持时以非零退出码退出，且不产生任何产物。

用法:
  python shot_cover.py --input <视频路径> --output <输出目录>
      [--diff-threshold 28] [--black-threshold 18] [--min-shot-ms 250] [--jpeg-quality 92]

设计约束:
  - 仅以只读方式打开源视频，全程不写入/修改源文件；
  - 仅依赖 opencv-python 与 numpy；
  - 全部时间戳以「帧序号 / FPS」计算并四舍五入为整数毫秒。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# 常量与数据结构
# ---------------------------------------------------------------------------

# 分析用的缩放宽度（灰度），控制内存与计算量；等比缩放不影响镜头边界判定
ANALYSIS_WIDTH = 160

JPEG_TEMPLATE = "shot_{idx:03d}_{ms:010d}ms.jpg"
REPORT_FILENAME = "report.json"


class VideoDecodeError(Exception):
    """视频无法打开/解码/格式不支持时抛出。"""


@dataclass
class FrameInfo:
    """第一遍扫描中每帧的轻量统计信息。"""
    index: int          # 帧序号，从 0 开始
    timestamp_ms: int   # 该帧展示时间戳（整数毫秒）
    mean_gray: float    # 缩放灰度图平均亮度（0~255）
    small_gray: np.ndarray  # 缩放后的灰度帧（用于差异计算）


@dataclass
class Shot:
    """一个最终镜头（黑帧不作为镜头内容，仅用于镜头分界）。"""
    shot_index: int
    start_frame: int
    end_frame: int          # 含该帧
    fps: float
    black_frame_count: int = 0
    frame_count: int = 0
    candidate_frame: Optional[int] = None
    candidate_timestamp_ms: Optional[int] = None
    candidate_filename: Optional[str] = None

    @property
    def start_ms(self) -> int:
        return round(self.start_frame / self.fps * 1000)

    @property
    def end_ms(self) -> int:
        """镜头结束时间 = 最后一帧时间戳 + 一帧时长（半开区间上界）。"""
        return round((self.end_frame + 1) / self.fps * 1000)


@dataclass
class ExtractionResult:
    video_path: str
    fps: float
    frame_count: int
    duration_ms: int
    shots: List[Shot] = field(default_factory=list)
    candidates: List[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 视频打开与第一遍扫描
# ---------------------------------------------------------------------------

def _open_capture(video_path: str) -> cv2.VideoCapture:
    """以只读方式打开视频，依次尝试 FFMPEG / MSMF / 默认后端。"""
    if not os.path.isfile(video_path):
        raise VideoDecodeError(f"输入文件不存在: {video_path}")

    last_err = None
    for backend in (cv2.CAP_FFMPEG, cv2.CAP_MSMF, cv2.CAP_ANY):
        cap = cv2.VideoCapture(video_path, backend)
        if cap.isOpened():
            # 必须能读到至少一帧，否则视为不可解码
            ok, _ = cap.read()
            if ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                return cap
            last_err = "已打开但无法读取任何帧"
        else:
            last_err = "VideoCapture 打开失败"
        cap.release()
    raise VideoDecodeError(
        f"视频无法解码或格式不支持: {video_path}（{last_err}）"
    )


def _frame_timestamp_ms(index: int, fps: float) -> int:
    """帧时间戳 = 帧序号 / FPS，四舍五入为整数毫秒。"""
    return int(round(index / fps * 1000.0))


def scan_frames(cap: cv2.VideoCapture) -> "tuple[float, int, List[FrameInfo]]":
    """顺序解码全部帧，返回 (fps, 总帧数, 每帧统计)。"""
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps <= 0 or not np.isfinite(fps):
        raise VideoDecodeError("视频帧率无效或无法读取（FPS <= 0），不支持该文件")

    frames: List[FrameInfo] = []
    index = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        if w != ANALYSIS_WIDTH:
            scale = ANALYSIS_WIDTH / float(w)
            small = cv2.resize(
                gray, (ANALYSIS_WIDTH, max(1, int(round(h * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        else:
            small = gray
        frames.append(
            FrameInfo(
                index=index,
                timestamp_ms=_frame_timestamp_ms(index, fps),
                mean_gray=float(small.mean()),
                small_gray=small,
            )
        )
        index += 1

    if not frames:
        raise VideoDecodeError("视频不包含任何可解码帧，文件可能已损坏")

    return float(fps), len(frames), frames


# ---------------------------------------------------------------------------
# 镜头切分
# ---------------------------------------------------------------------------

def _is_black(info: FrameInfo, black_threshold: float) -> bool:
    return info.mean_gray < black_threshold


def detect_cut_positions(
    frames: List[FrameInfo],
    black_threshold: float,
    diff_threshold: float,
    min_shot_ms: int,
    fps: float,
) -> List[int]:
    """
    检测硬切位置。返回切点列表，每个切点是新镜头的首帧序号。

    判定规则（同时满足）:
      1. 相邻帧灰度平均绝对差 > diff_threshold；
      2. 差异显著高于局部基线（3 倍邻域中位数），抑制渐变/压缩噪声误报；
      3. 切点两侧均非黑帧（黑场的进入/退出由 build_shots 处理为镜头分界，
         而非内容硬切）；
      4. 与上一个切点的间距不小于最小镜头时长。
    """
    n = len(frames)
    if n < 2:
        return []

    diffs = np.empty(n - 1, dtype=np.float64)
    for i in range(n - 1):
        diffs[i] = np.abs(
            frames[i].small_gray.astype(np.float32)
            - frames[i + 1].small_gray.astype(np.float32)
        ).mean()

    min_gap = max(1, int(round(fps * min_shot_ms / 1000.0)))
    cuts: List[int] = []
    last_cut = 0  # 上一个切点（首帧序号），0 表示视频开头

    for i in range(n - 1):
        lo, hi = max(0, i - 12), min(n - 1, i + 12)
        local = np.concatenate((diffs[lo:i], diffs[i + 1:hi + 1]))
        local_median = float(np.median(local)) if local.size else 0.0

        is_hard_change = (
            diffs[i] > diff_threshold
            and diffs[i] > 3.0 * max(local_median, 1.0)
        )
        both_lit = (
            not _is_black(frames[i], black_threshold)
            and not _is_black(frames[i + 1], black_threshold)
        )
        far_enough = (i + 1 - last_cut) >= min_gap

        if is_hard_change and both_lit and far_enough:
            cuts.append(i + 1)
            last_cut = i + 1

    return cuts


def build_shots(
    frames: List[FrameInfo],
    fps: float,
    cuts: List[int],
    black_threshold: float,
    min_shot_ms: int,
) -> List[Shot]:
    """
    构建最终镜头，规则:
      - 黑帧不作为镜头内容：以黑帧为分隔，黑场两侧的亮画面各自成镜头
        （黑场视为镜头分界，保证黑帧永远不会被选为候选）；
      - 首尾黑场直接丢弃，中间黑场只起分隔作用；
      - 过短的非黑碎片（极短片段）并入相邻镜头，规避噪声分段；
      - 全黑视频返回空列表。
    """
    n = len(frames)
    min_frames = max(1, int(round(fps * min_shot_ms / 1000.0)))

    # 1) 在硬切切点之外，把每个“亮->黑/黑->亮”转换位置也作为边界
    boundaries = {0, n}
    boundaries.update(cuts)
    for i in range(n - 1):
        b0 = _is_black(frames[i], black_threshold)
        b1 = _is_black(frames[i + 1], black_threshold)
        if b0 != b1:
            boundaries.add(i + 1)
    boundaries = sorted(boundaries)

    # 2) 切出原始段并丢弃全黑段
    lit_segments: List[List[int]] = []
    for k in range(len(boundaries) - 1):
        start, end = boundaries[k], boundaries[k + 1] - 1
        if start > end:
            continue
        black_cnt = sum(
            1 for j in range(start, end + 1)
            if _is_black(frames[j], black_threshold)
        )
        if black_cnt == end - start + 1:
            continue  # 黑场段：仅起分界作用，不成为镜头
        lit_segments.append([start, end])

    if not lit_segments:
        return []  # 全黑视频

    # 3) 过短的亮碎片并入相邻段（优先向前合并，首段则向后合并）
    merged = [lit_segments[0]]
    for seg in lit_segments[1:]:
        if seg[1] - seg[0] + 1 < min_frames:
            merged[-1][1] = seg[1]
        elif merged[-1][1] - merged[-1][0] + 1 < min_frames:
            merged[-1][1] = seg[1]
        else:
            merged.append(seg)

    # 4) 编号成镜头（序号从 0 连续递增）
    shots: List[Shot] = []
    for start, end in merged:
        black_cnt = sum(
            1 for j in range(start, end + 1)
            if _is_black(frames[j], black_threshold)
        )
        shots.append(
            Shot(
                shot_index=len(shots),
                start_frame=start,
                end_frame=end,
                fps=fps,
                black_frame_count=black_cnt,
                frame_count=end - start + 1,
            )
        )
    return shots


# ---------------------------------------------------------------------------
# 候选帧选择
# ---------------------------------------------------------------------------

def choose_candidate(
    shot: Shot,
    frames: List[FrameInfo],
    black_threshold: float,
) -> Optional[int]:
    """
    在镜头中部区域挑选候选帧:
      1. 优先取镜头时间中点所在帧；
      2. 中点为黑帧时，从中点向两侧对称搜索，选亮度最高的非黑帧；
      3. 镜头内不存在任何非黑帧则返回 None。
    """
    mid = (shot.start_frame + shot.end_frame) // 2
    if not _is_black(frames[mid], black_threshold):
        return mid

    best: Optional[int] = None
    best_brightness = -1.0
    max_radius = max(mid - shot.start_frame, shot.end_frame - mid)
    for radius in range(1, max_radius + 1):
        for j in (mid - radius, mid + radius):
            if j < shot.start_frame or j > shot.end_frame:
                continue
            if _is_black(frames[j], black_threshold):
                continue
            if frames[j].mean_gray > best_brightness:
                best_brightness = frames[j].mean_gray
                best = j
        if best is not None:
            # 找到距离中点最近的非黑帧层级即停止（同层取最亮者）
            return best
    return None


def write_candidate_jpeg(
    cap: cv2.VideoCapture,
    frame_index: int,
    out_path: str,
    jpeg_quality: int,
) -> None:
    """跳转到指定帧并以 JPEG 写出。"""
    cap.set(cv2.CAP_PROP_POS_FRAMES, float(frame_index))
    ok, frame = cap.read()
    if not ok:
        raise VideoDecodeError(f"候选帧 {frame_index} 读取失败，文件可能已损坏")
    params = [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)]
    if not cv2.imwrite(out_path, frame, params):
        raise IOError(f"JPEG 写入失败: {out_path}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_extraction(
    video_path: str,
    output_dir: str,
    diff_threshold: float = 28.0,
    black_threshold: float = 18.0,
    min_shot_ms: int = 250,
    jpeg_quality: int = 92,
) -> ExtractionResult:
    cap = _open_capture(video_path)
    try:
        fps, frame_count, frames = scan_frames(cap)
        duration_ms = round(frame_count / fps * 1000.0)

        cuts = detect_cut_positions(
            frames, black_threshold, diff_threshold, min_shot_ms, fps
        )
        shots = build_shots(frames, fps, cuts, black_threshold, min_shot_ms)

        candidates: List[dict] = []
        used_names: set = set()
        for shot in shots:
            cand = choose_candidate(shot, frames, black_threshold)
            if cand is None:
                continue  # 理论上 build_shots 已剔除全黑镜头，这里双保险
            ts_ms = _frame_timestamp_ms(cand, fps)
            filename = JPEG_TEMPLATE.format(idx=shot.shot_index, ms=ts_ms)
            assert filename not in used_names, "候选帧文件名出现重复"
            used_names.add(filename)

            shot.candidate_frame = cand
            shot.candidate_timestamp_ms = ts_ms
            shot.candidate_filename = filename

            os.makedirs(output_dir, exist_ok=True)
            write_candidate_jpeg(
                cap, cand, os.path.join(output_dir, filename), jpeg_quality
            )
            candidates.append({
                "shot_index": shot.shot_index,
                "filename": filename,
                "timestamp_ms": ts_ms,
            })

        report = {
            "video_path": os.path.abspath(video_path),
            "duration_ms": int(duration_ms),
            "frame_count": int(frame_count),
            "fps": round(fps, 4),
            "shot_count": len(shots),
            "candidate_count": len(candidates),
            "shots": [
                {
                    "shot_index": s.shot_index,
                    "start_ms": s.start_ms,
                    "end_ms": s.end_ms,
                    "start_frame": s.start_frame,
                    "end_frame": s.end_frame,
                    "frame_count": s.frame_count,
                }
                for s in shots
            ],
            "candidates": candidates,
        }
        os.makedirs(output_dir, exist_ok=True)
        report_path = os.path.join(output_dir, REPORT_FILENAME)
        with open(report_path, "w", encoding="utf-8") as fp:
            json.dump(report, fp, ensure_ascii=False, indent=2)

        return ExtractionResult(
            video_path=os.path.abspath(video_path),
            fps=fps,
            frame_count=frame_count,
            duration_ms=duration_ms,
            shots=shots,
            candidates=candidates,
        )
    finally:
        cap.release()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="视频镜头封面候选帧提取：切分镜头、每镜头输出一张非黑 JPEG 候选帧与 JSON 报告。"
    )
    parser.add_argument("--input", "-i", required=True, help="输入视频路径（本地 MP4 等）")
    parser.add_argument("--output", "-o", required=True, help="输出目录（不存在则自动创建）")
    parser.add_argument("--diff-threshold", type=float, default=28.0,
                        help="硬切帧间灰度平均差阈值（默认 28）")
    parser.add_argument("--black-threshold", type=float, default=18.0,
                        help="黑场判定的平均灰度阈值（默认 18）")
    parser.add_argument("--min-shot-ms", type=int, default=250,
                        help="最小镜头时长毫秒，短于该值的碎片并入相邻镜头（默认 250）")
    parser.add_argument("--jpeg-quality", type=int, default=92,
                        help="JPEG 质量 1~100（默认 92）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _build_arg_parser().parse_args(argv)
    try:
        result = run_extraction(
            video_path=args.input,
            output_dir=args.output,
            diff_threshold=args.diff_threshold,
            black_threshold=args.black_threshold,
            min_shot_ms=args.min_shot_ms,
            jpeg_quality=args.jpeg_quality,
        )
    except VideoDecodeError as exc:
        # 需求 (7)：无法解码/不支持时明确提示，非零退出，且不产出候选帧与报告
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except (OSError, IOError) as exc:
        print(f"[错误] 文件读写失败: {exc}", file=sys.stderr)
        return 3

    print(
        f"摘要: 检测镜头数 = {len(result.shots)}，"
        f"生成候选帧数 = {len(result.candidates)}，"
        f"视频总时长 = {result.duration_ms} ms，"
        f"输出目录 = {os.path.abspath(args.output)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
