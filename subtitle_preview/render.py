"""预览帧渲染与说明 JSON 生成。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .layout import (
    Placement,
    get_font,
    plan_subtitle,
    resolve_font_path,
    two_line_capacity_height,
    usable_region,
)
from .models import InputError, Manifest, Rect

REPORT_FILENAME = "render_report.json"
PNG_COMPRESS_LEVEL = 6
BASE_PNG_COMPRESS_LEVEL = 9


class OutputError(Exception):
    """输出目录不可创建/不可写时抛出。"""


def _posix(path: str) -> str:
    return path.replace("\\", "/")


def _output_name(index: int, time_ms: int) -> str:
    return f"preview_{index:04d}_{time_ms:09d}ms.png"


def prepare_output_dir(output_dir: Path) -> None:
    """创建输出目录并探测可写性；失败时不留下任何文件。"""
    if output_dir.exists() and not output_dir.is_dir():
        raise OutputError(f"输出路径已存在但不是目录：{output_dir}")
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputError(f"无法创建输出目录 {output_dir}：{exc}") from exc
    probe = None
    try:
        fd, probe_name = tempfile.mkstemp(
            prefix=".write-probe-", suffix=".tmp", dir=output_dir
        )
        os.close(fd)
        probe = Path(probe_name)
    except OSError as exc:
        raise OutputError(f"输出目录不可写 {output_dir}：{exc}") from exc
    finally:
        if probe is not None:
            try:
                probe.unlink()
            except OSError:
                pass


def _atomic_write(path: Path, write_fn) -> None:
    """在同一目录写临时文件再替换，避免留下半成品。"""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        write_fn(tmp_path)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


@dataclass
class FrameOutcome:
    frame: Any
    output_name: str | None
    subtitle_indices: list[int]
    problem: str | None


def _load_frame_rgb(path: Path, canvas: tuple[int, int]) -> tuple[Image.Image, str | None]:
    try:
        with Image.open(path) as image:
            image.load()
            base = image.copy()
    except FileNotFoundError:
        raise InputError("帧文件不存在") from None
    except UnidentifiedImageError:
        raise InputError("PNG 无法解码（文件不是可识别的图像）") from None
    except (OSError, ValueError) as exc:
        raise InputError(f"PNG 无法解码：{exc}") from exc
    note = None
    if base.size != canvas:
        note = (
            f"帧尺寸 {base.size[0]}x{base.size[1]} 与画布 "
            f"{canvas[0]}x{canvas[1]} 不一致，按实际帧尺寸叠加字幕"
        )
    if base.mode not in ("RGB", "RGBA"):
        base = base.convert("RGBA" if "A" in base.getbands() else "RGB")
    return base, note


def _draw_placements(
    image: Image.Image,
    placements: list[Placement],
    manifest: Manifest,
    font,
) -> None:
    style = manifest.style
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    for placement in placements:
        if not placement.placed or placement.box is None or placement.metrics is None:
            continue
        metrics = placement.metrics
        x = placement.box.left + style.padding
        y = placement.box.top + style.padding
        for i, line in enumerate(metrics.lines):
            draw.text(
                (x, y + i * metrics.line_step),
                line.text,
                font=font,
                fill=style.fill,
                stroke_width=style.stroke_width,
                stroke_fill=style.stroke_fill,
                anchor="la",
            )


def render(manifest: Manifest, output_dir: Path) -> dict[str, Any]:
    prepare_output_dir(output_dir)

    font_path = resolve_font_path(manifest.style, manifest.base_dir)
    font = get_font(manifest.style, manifest.base_dir)

    placements: list[Placement] = [
        plan_subtitle(manifest, subtitle, font) for subtitle in manifest.subtitles
    ]
    # 说明按字幕起始毫秒升序，起始相同则按输入顺序。
    ordered = sorted(placements, key=lambda p: (p.subtitle.start_ms, p.subtitle.index))

    placements_by_frame: dict[int, list[Placement]] = {}
    for placement in ordered:
        if not placement.placed:
            continue
        for frame in manifest.frames:
            if placement.subtitle.start_ms <= frame.time_ms <= placement.subtitle.end_ms:
                placements_by_frame.setdefault(frame.index, []).append(placement)

    frame_outcomes: list[FrameOutcome] = []
    written: list[Path] = []
    try:
        for frame in manifest.frames:
            active = placements_by_frame.get(frame.index, [])
            output_name = _output_name(frame.index, frame.time_ms)
            target = output_dir / output_name
            try:
                image, size_note = _load_frame_rgb(frame.path, manifest.canvas)
            except InputError as exc:
                frame_outcomes.append(
                    FrameOutcome(
                        frame,
                        None,
                        [p.subtitle.index for p in active],
                        str(exc),
                    )
                )
                continue
            if active:
                _draw_placements(image, active, manifest, font)
                compress_level = PNG_COMPRESS_LEVEL
            else:
                compress_level = BASE_PNG_COMPRESS_LEVEL
            _atomic_write(
                target,
                lambda tmp, img=image, level=compress_level: img.save(
                    tmp, format="PNG", compress_level=level
                ),
            )
            image.close()
            written.append(target)
            frame_outcomes.append(
                FrameOutcome(
                    frame,
                    output_name,
                    [p.subtitle.index for p in active],
                    size_note,
                )
            )
    except BaseException:
        for path in written:
            try:
                path.unlink()
            except OSError:
                pass
        raise

    report = build_report(manifest, ordered, frame_outcomes, font_path, font)
    report_path = output_dir / REPORT_FILENAME
    try:
        _atomic_write(
            report_path,
            lambda tmp: tmp.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            ),
        )
    except BaseException:
        for path in written:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return report


def _rect_json(rect: Rect | None) -> dict[str, int] | None:
    if rect is None:
        return None
    data = rect.as_dict()
    data["width"] = rect.width
    data["height"] = rect.height
    return data


def build_report(
    manifest: Manifest,
    ordered: list[Placement],
    frame_outcomes: list[FrameOutcome],
    font_path: str | None,
    font,
) -> dict[str, Any]:
    output_by_frame = {outcome.frame.index: outcome.output_name for outcome in frame_outcomes}

    subtitle_entries: list[dict[str, Any]] = []
    for placement in ordered:
        sub = placement.subtitle
        frames_json = []
        for frame in manifest.frames:
            if not (sub.start_ms <= frame.time_ms <= sub.end_ms):
                continue
            output_name = output_by_frame.get(frame.index)
            frames_json.append(
                {
                    "index": frame.index,
                    "time_ms": frame.time_ms,
                    "output": output_name,
                    "rendered": output_name is not None and placement.placed,
                }
            )
        subtitle_entries.append(
            {
                "index": sub.index,
                "start_ms": sub.start_ms,
                "end_ms": sub.end_ms,
                "text": sub.text,
                "onset_ms": sub.onset_ms,
                "onset_offset_ms": placement.onset_offset_ms,
                "offset_exceeds_threshold": placement.onset_offset_large,
                "placed": placement.placed,
                "result": placement.result,
                "reason": placement.reason,
                "box": _rect_json(placement.box),
                "frames": frames_json,
            }
        )

    frame_entries = []
    for outcome in frame_outcomes:
        frame_entries.append(
            {
                "index": outcome.frame.index,
                "time_ms": outcome.frame.time_ms,
                "path": _posix(outcome.frame.path_raw),
                "output": outcome.output_name,
                "subtitle_indices": outcome.subtitle_indices,
                "problem": outcome.problem,
            }
        )

    problems = [entry["problem"] for entry in frame_entries if entry["problem"]]
    unplaced = [entry["text"] for entry in subtitle_entries if not entry["placed"]]
    large_offsets = [entry["text"] for entry in subtitle_entries if entry["result"] == "偏移过大"]

    style = manifest.style
    region = usable_region(manifest.safe_area, manifest.panel)
    capacity = two_line_capacity_height(font, style)
    return {
        "canvas": {"width": manifest.canvas[0], "height": manifest.canvas[1]},
        "safe_area": _rect_json(manifest.safe_area),
        "panel": {
            "status": manifest.panel_status,
            "rect": _rect_json(manifest.panel),
        },
        "usable_region": _rect_json(region),
        "two_line_capacity_height": capacity,
        "text_style": {
            "font_path": _posix(font_path) if font_path else None,
            "font_size": style.font_size,
            "line_spacing": style.line_spacing,
            "padding": style.padding,
            "stroke_width": style.stroke_width,
            "max_lines": 2,
            "onset_review_threshold_ms": style.onset_review_threshold_ms,
        },
        "frames": frame_entries,
        "subtitles": subtitle_entries,
        "warnings": list(manifest.warnings) + problems,
        "summary": {
            "frame_count": len(frame_entries),
            "rendered_frame_count": sum(1 for e in frame_entries if e["output"]),
            "subtitle_count": len(subtitle_entries),
            "placed_count": sum(1 for e in subtitle_entries if e["placed"]),
            "unplaced_count": len(unplaced),
            "missing_or_broken_frame_count": len(problems),
            "large_onset_offset_count": len(large_offsets),
        },
    }
