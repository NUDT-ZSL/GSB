# -*- coding: utf-8 -*-
"""音画时序对照审计工具。

输入清单 JSON（帧率、总时长、镜头区间[帧]、节拍点[毫秒]、转场区间[帧]），
统一换算到帧口径后完成以下分析并产出：
  1. 音画时序对照报告 report.json（含被吞节拍、镜头首尾缺帧、时序异常、片尾余量）；
  2. 每个镜头一张转场露出标记图 PNG（按镜头时长均匀取样拼接的横向帧序列，
     被转场遮住的位置加红色遮罩，可见位置为绿色）。

口径约定（确定性、可手算复核）：
  - 所有帧区间均为半开区间 [start, end)，即包含 start 帧、不包含 end 帧；
  - 毫秒 -> 帧：frame = floor(t_ms * fps / 1000)，整数运算，不做四舍五入；
  - 总帧数 total_frames = floor(duration_ms * fps / 1000)；
  - 节拍越界判定：frame < 0 或 frame >= total_frames。

用法：
  python -I av_sync_audit.py <manifest.json> <output_dir>
非法清单（帧率为零/负、总时长为负、JSON 无法解析等）整体报错退出，
退出码非 0，且输出目录不产生任何文件。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw

# ---- 标记图参数（确定性，不依赖运行时间/随机数） ----
FRAME_W = 40
FRAME_H = 64
SAMPLES_PER_SHOT = 8
COLOR_VISIBLE = (46, 160, 67)       # 绿色：可见画面
COLOR_COVERED = (220, 50, 50)       # 红色：被转场遮住（缺露出）
COLOR_TEXT = (255, 255, 255)
COLOR_GRID = (20, 20, 20)


class ManifestError(Exception):
    """清单非法时抛出：调用方负责打印错误并以非零码退出。"""


@dataclass(frozen=True)
class Shot:
    id: str
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Transition:
    id: str
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


def _is_int(value: Any) -> bool:
    """严格整数判定：bool 不算整数（JSON 的 true/false 不能当帧号）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool))


def ms_to_frame(t_ms: float, fps: float) -> int:
    """毫秒时间戳换算为帧序号：floor(t_ms * fps / 1000)。

    用真整除（负数也向负无穷取整）：-100ms @30fps -> floor(-3) = -3 帧。
    """
    return int((float(t_ms) * fps) // 1000)


def load_manifest(path: str) -> dict[str, Any]:
    """读取并解析清单 JSON；解析失败抛 ManifestError，不产出任何文件。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise ManifestError(f"清单文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"清单无法解析（不是合法 JSON）: {exc}") from exc
    except OSError as exc:
        raise ManifestError(f"清单读取失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("清单无法解析：根节点必须是 JSON 对象")
    return data


def validate_and_normalize(data: dict[str, Any]) -> dict[str, Any]:
    """校验清单并归一化为内部结构。任何非法项都在写文件之前抛错。"""
    # 1) 帧率
    if "fps" not in data or not _is_number(data["fps"]):
        raise ManifestError("帧率非法：字段 fps 必须是数字")
    fps = float(data["fps"])
    if fps <= 0:
        raise ManifestError("帧率非法：fps 必须大于 0")

    # 2) 总时长
    if "duration_ms" not in data or not _is_number(data["duration_ms"]):
        raise ManifestError("总时长非法：字段 duration_ms 必须是数字")
    duration_ms = float(data["duration_ms"])
    if duration_ms < 0:
        raise ManifestError("总时长非法：duration_ms 不能为负数")

    total_frames = ms_to_frame(duration_ms, fps)

    # 3) 镜头
    raw_shots = data.get("shots", [])
    if not isinstance(raw_shots, list):
        raise ManifestError("镜头字段 shots 必须是数组")
    shots: list[Shot] = []
    shot_ids: set[str] = set()
    for i, item in enumerate(raw_shots):
        if not isinstance(item, dict):
            raise ManifestError(f"镜头 #{i} 必须是对象")
        sid = str(item.get("id", f"shot_{i}"))
        if sid in shot_ids:
            raise ManifestError(f"镜头 id 重复: {sid}")
        shot_ids.add(sid)
        start, end = item.get("start_frame"), item.get("end_frame")
        if not (_is_int(start) and _is_int(end)):
            raise ManifestError(f"镜头 {sid} 的 start_frame/end_frame 必须是整数")
        if start < 0 or end < 0:
            raise ManifestError(f"镜头 {sid} 帧号不能为负")
        if end <= start:
            raise ManifestError(f"镜头 {sid} 区间非法：end_frame 必须大于 start_frame")
        shots.append(Shot(sid, start, end))

    # 4) 转场
    raw_trans = data.get("transitions", [])
    if not isinstance(raw_trans, list):
        raise ManifestError("转场字段 transitions 必须是数组")
    transitions: list[Transition] = []
    trans_ids: set[str] = set()
    for i, item in enumerate(raw_trans):
        if not isinstance(item, dict):
            raise ManifestError(f"转场 #{i} 必须是对象")
        tid = str(item.get("id", f"trans_{i}"))
        if tid in trans_ids:
            raise ManifestError(f"转场 id 重复: {tid}")
        trans_ids.add(tid)
        start, end = item.get("start_frame"), item.get("end_frame")
        if not (_is_int(start) and _is_int(end)):
            raise ManifestError(f"转场 {tid} 的 start_frame/end_frame 必须是整数")
        if start < 0 or end < 0:
            raise ManifestError(f"转场 {tid} 帧号不能为负")
        if end <= start:
            raise ManifestError(f"转场 {tid} 区间非法：end_frame 必须大于 start_frame")
        transitions.append(Transition(tid, start, end))

    # 5) 节拍（毫秒）
    raw_beats = data.get("beats_ms", [])
    if not isinstance(raw_beats, list):
        raise ManifestError("节拍字段 beats_ms 必须是数组")
    beats: list[float] = []
    for i, t in enumerate(raw_beats):
        if not _is_number(t):
            raise ManifestError(f"节拍 #{i} 必须是数字（毫秒）")
        beats.append(float(t))

    return {
        "fps": fps,
        "duration_ms": duration_ms,
        "total_frames": total_frames,
        "shots": shots,
        "transitions": transitions,
        "beats_ms": beats,
    }


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并半开帧区间的并集，结果按起点排序且互不重叠。"""
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged: list[list[int]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:       # 相邻（end==start）也合并
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def frame_covered_by(frame: int, ranges: list[tuple[int, int]]) -> bool:
    return any(s <= frame < e for s, e in ranges)


def find_owner_shot(frame: int, shots: list[Shot]) -> str | None:
    """返回包含该帧的第一个镜头 id；无镜头包含返回 None。"""
    for shot in shots:
        if shot.start <= frame < shot.end:
            return shot.id
    return None


def analyze(norm: dict[str, Any]) -> dict[str, Any]:
    fps = norm["fps"]
    total_frames = norm["total_frames"]
    shots: list[Shot] = norm["shots"]
    transitions: list[Transition] = norm["transitions"]
    beats_ms: list[float] = norm["beats_ms"]

    trans_ranges = [(t.start, t.end) for t in transitions]

    # ---- 异常 1：镜头区间互相重叠（定位到首个重叠帧） ----
    anomalies: list[dict[str, Any]] = []
    overlap_pairs: set[tuple[str, str]] = set()
    for i in range(len(shots)):
        for j in range(i + 1, len(shots)):
            a, b = shots[i], shots[j]
            ov_start = max(a.start, b.start)
            ov_end = min(a.end, b.end)
            if ov_start < ov_end:
                key = tuple(sorted((a.id, b.id)))
                if key not in overlap_pairs:
                    overlap_pairs.add(key)
                    anomalies.append({
                        "type": "overlapping_shots",
                        "shot_ids": [a.id, b.id],
                        "overlap_start_frame": ov_start,
                        "overlap_end_frame": ov_end,
                        "location_frame": ov_start,
                        "overlap_frames": ov_end - ov_start,
                    })

    # ---- 镜头越出总时长（同样属于时序异常，给出定位帧） ----
    for shot in shots:
        if shot.start >= total_frames or shot.end > total_frames:
            anomalies.append({
                "type": "shot_out_of_duration",
                "shot_id": shot.id,
                "location_frame": max(shot.start, total_frames),
                "shot_start_frame": shot.start,
                "shot_end_frame": shot.end,
                "total_frames": total_frames,
            })

    # ---- 节拍换算 + 异常 2：节拍超出总时长 ----
    beat_records: list[dict[str, Any]] = []
    swallowed: list[dict[str, Any]] = []
    for idx, t_ms in enumerate(beats_ms):
        frame = ms_to_frame(t_ms, fps)
        in_range = 0 <= frame < total_frames
        record = {
            "beat_index": idx,
            "time_ms": t_ms,
            "frame": frame,
            "in_duration": in_range,
            "owner_shot_id": find_owner_shot(frame, shots) if in_range else None,
            "swallowed": False,
            "transition_ids": [],
        }
        if not in_range:
            anomalies.append({
                "type": "beat_out_of_duration",
                "beat_index": idx,
                "time_ms": t_ms,
                "location_frame": frame,
                "total_frames": total_frames,
            })
        else:
            hit_trans = [t.id for t in transitions if t.start <= frame < t.end]
            if hit_trans:
                record["swallowed"] = True
                record["transition_ids"] = hit_trans
                swallowed.append({
                    "beat_index": idx,
                    "time_ms": t_ms,
                    "frame": frame,
                    "owner_shot_id": record["owner_shot_id"],
                    "transition_ids": hit_trans,
                })
        beat_records.append(record)

    # ---- 逐镜头：首尾缺帧 + 转场覆盖并集 ----
    shot_reports: list[dict[str, Any]] = []
    for shot in shots:
        head_ranges = [(t.start, t.end) for t in transitions
                       if t.start <= shot.start < t.end]
        tail_ranges = [(t.start, t.end) for t in transitions
                       if t.start < shot.end <= t.end]
        head_missing = sum(min(t.end, shot.end) - max(t.start, shot.start)
                           for t in transitions if t.start <= shot.start < t.end)
        tail_missing = sum(min(t.end, shot.end) - max(t.start, shot.start)
                           for t in transitions if t.start < shot.end <= t.end)

        covered_in_shot = merge_ranges(
            [(max(s, shot.start), min(e, shot.end))
             for s, e in trans_ranges if s < shot.end and e > shot.start]
        )
        covered_count = sum(e - s for s, e in covered_in_shot)

        shot_reports.append({
            "shot_id": shot.id,
            "start_frame": shot.start,
            "end_frame": shot.end,
            "length_frames": shot.length,
            "head_missing_frames": head_missing,
            "tail_missing_frames": tail_missing,
            "covered_frame_ranges": [[s, e] for s, e in covered_in_shot],
            "covered_frames": covered_count,
            "visible_frames": shot.length - covered_count,
            "has_head_or_tail_gap": head_missing > 0 or tail_missing > 0,
        })

    # ---- 片尾音乐是否提前结束：最后一个节拍到片尾的帧余量 ----
    if beat_records:
        last_beat = max(beat_records, key=lambda r: r["frame"])
        tail_silence_frames = max(0, total_frames - 1 - last_beat["frame"])
    else:
        last_beat = None
        tail_silence_frames = total_frames

    return {
        "fps": fps,
        "duration_ms": norm["duration_ms"],
        "total_frames": total_frames,
        "frame_rule": "frame = floor(time_ms * fps / 1000); intervals are half-open [start, end)",
        "beat_count": len(beats_ms),
        "beats": beat_records,
        "swallowed_beats": swallowed,
        "shots": shot_reports,
        "anomalies": anomalies,
        "last_beat_frame": None if last_beat is None else last_beat["frame"],
        "tail_silence_frames": tail_silence_frames,
    }


def sample_positions(shot: Shot, k: int = SAMPLES_PER_SHOT) -> list[int]:
    """在 [start, end) 内均匀取 k 个帧位（镜头短于 k 则逐帧全取）。

    pos = start + floor(i * length / k_actual)，纯整数运算，结果确定。
    """
    n = min(k, shot.length)
    return [shot.start + (i * shot.length) // n for i in range(n)]


def render_shot_marker(shot: Shot, covered: list[tuple[int, int]],
                       out_path: str) -> dict[str, Any]:
    """绘制单镜头转场露出标记图并保存，返回取样元数据（供报告/测试核对）。"""
    positions = sample_positions(shot)
    width = len(positions) * FRAME_W
    img = Image.new("RGB", (width, FRAME_H), COLOR_GRID)
    draw = ImageDraw.Draw(img)
    samples_meta = []
    for i, frame in enumerate(positions):
        x0 = i * FRAME_W
        is_covered = frame_covered_by(frame, covered)
        color = COLOR_COVERED if is_covered else COLOR_VISIBLE
        draw.rectangle([x0 + 1, 1, x0 + FRAME_W - 2, FRAME_H - 2], fill=color)
        draw.text((x0 + 3, 4), str(frame), fill=COLOR_TEXT)
        draw.text((x0 + 3, FRAME_H - 16), "X" if is_covered else "O",
                  fill=COLOR_TEXT)
        samples_meta.append({
            "sample_index": i,
            "frame": frame,
            "covered": is_covered,
            "x0": x0,
        })
    img.save(out_path, format="PNG")
    return {
        "shot_id": shot.id,
        "image_file": os.path.basename(out_path),
        "image_width": width,
        "image_height": FRAME_H,
        "frame_width": FRAME_W,
        "sample_count": len(positions),
        "samples": samples_meta,
    }


def run(manifest_path: str, output_dir: str) -> dict[str, Any]:
    """完整流程：校验全部通过后才创建输出目录并写文件。"""
    data = load_manifest(manifest_path)          # 解析失败 -> 抛错，无文件
    norm = validate_and_normalize(data)          # 帧率/时长非法 -> 抛错，无文件
    os.makedirs(output_dir, exist_ok=True)       # 至此清单确认合法
    report = analyze(norm)

    markers: list[dict[str, Any]] = []
    for shot in norm["shots"]:
        covered = [(max(s, shot.start), min(e, shot.end))
                   for s, e in [(t.start, t.end) for t in norm["transitions"]]
                   if s < shot.end and e > shot.start]
        covered = merge_ranges(covered)
        out_path = os.path.join(output_dir, f"marker_{shot.id}.png")
        markers.append(render_shot_marker(shot, covered, out_path))
    report["marker_images"] = markers

    report_path = os.path.join(output_dir, "report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return report


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("用法: python -I av_sync_audit.py <manifest.json> <output_dir>",
              file=sys.stderr)
        return 2
    try:
        report = run(argv[1], argv[2])
    except ManifestError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(f"审计完成：镜头 {len(report['shots'])} 个，"
          f"被吞节拍 {len(report['swallowed_beats'])} 条，"
          f"时序异常 {len(report['anomalies'])} 项，"
          f"标记图 {len(report['marker_images'])} 张 -> {argv[2]}")
    return 0


if __name__ == "__main__":
    # Windows 控制台默认 GBK，显式切换标准流为 UTF-8，保证中文错误信息可被外部解析
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv))
