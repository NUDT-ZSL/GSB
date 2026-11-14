"""核心检测:帧换算、转场相交、字幕互叠、时间倒置、安全区越界。

约定:
- 帧换算统一为 round(毫秒 * fps / 1000),报告与预览图共用同一结果。
- 帧区间为闭区间 [start_frame, end_frame],相交判定为 s1 <= e2 且 s2 <= e1。
- 结束帧不晚于起始帧(end <= start)的字幕为时间倒置,仅入错误列表,
  不参与转场/互叠/安全区检测,也不生成预览图。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from .parsers import Canvas, Subtitle, Transition
from .render import load_font, render_preview, text_width, wrap_text


def to_frame(ms: int, fps: float) -> int:
    """毫秒时间戳 -> 帧序号,统一四舍五入取整。"""
    return int(round(ms * fps / 1000.0))


def intervals_intersect(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def analyze(
    subtitles: list[Subtitle],
    transitions: Optional[list[Transition]],
    canvas: Canvas,
    frames_dir: Optional[str] = None,
) -> dict:
    """执行全部检测,返回报告主体(subtitles/errors/summary)。

    transitions 为 None 或空列表时跳过转场检测,其余检测不受影响。
    """
    font = load_font(canvas)
    mx, my = canvas.margin_x, canvas.margin_y
    safe_w = canvas.width - 2 * mx
    safe_h = canvas.height - 2 * my

    errors: list[dict] = []
    valid: list[dict] = []  # 通过时间倒置检查的字幕工作记录

    # --- 帧换算 + 时间倒置 -------------------------------------------------
    for sub in subtitles:
        start_f = to_frame(sub.start_ms, canvas.fps)
        end_f = to_frame(sub.end_ms, canvas.fps)
        rec = {"sub": sub, "start_frame": start_f, "end_frame": end_f}
        if end_f <= start_f:
            errors.append({
                "type": "time_inversion",
                "subtitle_index": sub.index,
                "source_order": sub.order,
                "start_frame": start_f,
                "end_frame": end_f,
                "message": f"结束帧 {end_f} 不晚于起始帧 {start_f},仅列入错误,不参与其他检测",
            })
        else:
            valid.append(rec)

    # --- 字幕互叠(仅有效字幕,闭区间相交) ---------------------------------
    overlap_pairs: list[tuple[int, int]] = []
    overlap_map: dict[int, set[int]] = defaultdict(set)
    for i in range(len(valid)):
        for j in range(i + 1, len(valid)):
            a, b = valid[i], valid[j]
            if intervals_intersect((a["start_frame"], a["end_frame"]),
                                   (b["start_frame"], b["end_frame"])):
                ia, ib = a["sub"].index, b["sub"].index
                overlap_pairs.append((ia, ib))
                overlap_map[ia].add(ib)
                overlap_map[ib].add(ia)

    # --- 逐条组装报告 ------------------------------------------------------
    entries: list[dict] = []
    n_transition_conflicts = 0
    n_safe_area_violations = 0
    for rec in valid:
        sub: Subtitle = rec["sub"]
        sf, ef = rec["start_frame"], rec["end_frame"]

        # 转场相交(清单缺失/为空时跳过)
        transition_conflicts = []
        if transitions:
            for t in transitions:
                if intervals_intersect((sf, ef), (t.start_frame, t.end_frame)):
                    transition_conflicts.append({
                        "start_frame": t.start_frame,
                        "end_frame": t.end_frame,
                        "description": t.description,
                    })
        n_transition_conflicts += len(transition_conflicts)

        # 排版与安全区
        lines = wrap_text(sub.text, font, safe_w)
        text_w = max((text_width(font, ln) for ln in lines), default=0.0)
        text_h = len(lines) * canvas.line_height
        margins = {
            "left": int(round((canvas.width - text_w) / 2)),
            "right": int(round(canvas.width - text_w - (canvas.width - text_w) / 2)),
            "top": int(canvas.height - my - text_h),
            "bottom": int(my),
        }
        violation = (margins["left"] < mx or margins["right"] < mx
                     or margins["top"] < my or margins["bottom"] < my)
        if violation:
            n_safe_area_violations += 1

        # 代表帧 PNG(取字幕起始帧)
        preview = None
        if frames_dir is not None:
            import os
            preview = os.path.join(frames_dir, f"sub_{sub.order + 1:03d}_frame{sf}.png")
            render_preview(canvas, font, lines, preview, violation,
                           label=f"SUB #{sub.index}  frames {sf}-{ef}")
            try:
                preview = os.path.relpath(preview)
            except ValueError:
                pass  # 跨盘符时保留绝对路径

        entries.append({
            "index": sub.index,
            "source_order": sub.order,
            "text": sub.text,
            "start_frame": sf,
            "end_frame": ef,
            "lines": len(lines),
            "margins": margins,
            "conflicts": {
                "transitions": transition_conflicts,
                "overlaps_with": sorted(overlap_map.get(sub.index, ())),
                "safe_area_violation": violation,
            },
            "preview_png": preview,
        })

    summary = {
        "total_subtitles": len(subtitles),
        "valid_subtitles": len(valid),
        "time_inversions": len(errors),
        "transition_conflicts": n_transition_conflicts,
        "overlap_pairs": len(overlap_pairs),
        "safe_area_violations": n_safe_area_violations,
    }
    return {
        "canvas": {
            "width": canvas.width,
            "height": canvas.height,
            "fps": canvas.fps,
            "safe_area_ratio": canvas.ratio,
            "safe_area_ratio_source": ("config" if canvas.safe_area_ratio is not None
                                       else "default(10%)"),
            "safe_margin_x": mx,
            "safe_margin_y": my,
            "font_size": canvas.font_size,
            "line_height": canvas.line_height,
        },
        "subtitles": entries,
        "overlap_pairs": [[a, b] for a, b in overlap_pairs],
        "errors": errors,
        "summary": summary,
    }
