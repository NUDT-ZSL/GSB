"""Build the JSON verification report and the offline HTML preview page.

Both outputs are deterministic: no wall-clock values are written unless the
caller explicitly asks for a timestamp, so two consecutive runs produce
byte-identical files.
"""

from __future__ import annotations

import html
import json
from typing import Any

from . import SCHEMA_VERSION, __version__
from .batch import (
    BatchResult,
    FrameResult,
    VERDICT_COMPLETE,
    VERDICT_PARTIAL,
    VERDICT_UNEXPORTABLE,
    sha256,
)

VERDICT_LABELS = {
    VERDICT_COMPLETE: "全部完整（complete）",
    VERDICT_PARTIAL: "部分未完成（partial）",
    VERDICT_UNEXPORTABLE: "无法导出（unexportable）",
}

STATUS_LABELS = {
    "ok": "已导出",
    "invalid": "参数无效",
    "missing_file": "文件缺失",
    "corrupt": "无法解码",
}

PLACEMENT_LABELS = {
    "as_is": "原尺寸",
    "centered": "居中留白",
    "downscaled": "等比缩放",
}


def _number(value: Any) -> int | float:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _duration_display(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if float(value) >= 1000:
            return f"{_number(value)} ms（{float(value) / 1000:.3f} s）"
        return f"{_number(value)} ms"
    return "无效"


def _hex_rgba(rgba: tuple[int, int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}{:02X}".format(*rgba)


def _padding_sample(
    composited: Any,
) -> tuple[tuple[int, int] | None, tuple[int, int, int, int] | None]:
    """Find a pixel that is certainly padding, if any part of the canvas is."""
    image = composited.image
    left, top, right, bottom = composited.content_box
    width, height = image.size
    if left > 0:
        point = (0, height // 2)
    elif right < width:
        point = (width - 1, height // 2)
    elif top > 0:
        point = (width // 2, 0)
    elif bottom < height:
        point = (width // 2, height - 1)
    else:
        return None, None
    rgba = image.getpixel(point)
    if not isinstance(rgba, tuple) or len(rgba) != 4:  # pragma: no cover - RGBA always
        return None, None
    return point, rgba  # type: ignore[return-value]


def frame_entry(result: FrameResult) -> dict[str, Any]:
    spec = result.spec
    composited = result.composited
    entry: dict[str, Any] = {
        "index": spec.index if isinstance(spec.index, (int, str, type(None))) else str(spec.index),
        "ordinal": spec.ordinal,
        "source_file": spec.file_ref,
        "duration_ms": _number(spec.duration_ms) if spec.duration_valid else spec.duration_ms,
        "duration_valid": spec.duration_valid,
        "status": result.status,
        "status_label": STATUS_LABELS[result.status],
        "usable": result.succeeded,
        "error": (
            None
            if result.succeeded
            else {"code": result.error_code, "message": result.error_message}
        ),
        "output": result.png_relpath,
        "sha256": sha256(result.png_bytes) if result.png_bytes else None,
        "size_processing": None,
        "channel": None,
        "notes": [issue["message"] for issue in spec.issues],
    }

    if composited is not None:
        padding_point, padding_rgba = _padding_sample(composited)
        left, top, right, bottom = composited.content_box
        entry["size_processing"] = {
            "source_size": [composited.source_width, composited.source_height],
            "output_size": [composited.image.width, composited.image.height],
            "placement": composited.placement,
            "placement_label": PLACEMENT_LABELS[composited.placement],
            "scale": round(composited.scale, 6),
            "content_box": list(composited.content_box),
            "padding": {
                "left": left,
                "top": top,
                "right": composited.image.width - right,
                "bottom": composited.image.height - bottom,
            },
            "has_padding": padding_rgba is not None,
            "padding_sample_point": list(padding_point) if padding_point else None,
            "padding_rgba": list(padding_rgba) if padding_rgba else None,
            "padding_hex": _hex_rgba(padding_rgba) if padding_rgba else None,
        }
        entry["channel"] = {
            "source_mode": composited.source_mode,
            "source_format": composited.source_format,
            "has_alpha_channel": composited.has_alpha,
            "alpha_range": [composited.alpha_min, composited.alpha_max],
            "fully_opaque": composited.alpha_min >= 255,
            "output_mode": "RGBA",
            "exif_orientation": composited.orientation.exif_orientation,
            "exif_applied": composited.orientation.exif_applied,
        }
        channel_notes: list[str] = []
        if not composited.has_alpha:
            channel_notes.append("源图缺少透明通道，按整体不透明处理（alpha=255）。")
        elif composited.had_alpha_fully_opaque:
            channel_notes.append("源图带透明通道，但全部像素不透明。")
        if composited.orientation.exif_applied:
            channel_notes.append(
                f"按 EXIF Orientation={composited.orientation.exif_orientation} 旋正。"
            )
        channel_notes.extend(composited.notes)
        entry["notes"] = channel_notes

    return entry


def _verdict_reason(batch: BatchResult, verdict: str) -> str:
    if verdict == VERDICT_COMPLETE:
        return "描述数组中的所有帧均可解码并成功合成，序号连续且无重复。"
    if verdict == VERDICT_PARTIAL:
        parts: list[str] = []
        if batch.failed:
            labels = [
                f"位置 {frame.ordinal}（序号 {frame.spec.index}）{STATUS_LABELS[frame.status]}"
                for frame in batch.failed
            ]
            parts.append("以下帧未完成，其余帧已正常导出：" + "；".join(labels) + "。")
        sequence_problems: list[str] = []
        if batch.sequence.missing_indices:
            sequence_problems.append(
                "缺失序号 " + ", ".join(map(str, batch.sequence.missing_indices))
            )
        if batch.sequence.duplicate_indices:
            sequence_problems.append(
                "重复序号 " + ", ".join(map(str, batch.sequence.duplicate_indices))
            )
        if batch.sequence.declared_count_matches is False:
            sequence_problems.append("声明帧数与实际条目不一致")
        if sequence_problems:
            parts.append("序号问题：" + "；".join(sequence_problems))
        return " ".join(parts) or "部分帧未完成。"
    return "没有任何帧成功导出，整批不可交付。"


def build_report(batch: BatchResult, *, timestamp: str | None = None) -> dict[str, Any]:
    spec = batch.spec
    entries = [frame_entry(result) for result in batch.frames]
    succeeded = batch.succeeded
    failed = batch.failed
    verdict = batch.verdict

    summary_notes: list[str] = []
    if batch.sequence.duplicate_indices:
        summary_notes.append(
            "重复帧序号：" + ", ".join(map(str, batch.sequence.duplicate_indices))
        )
    if batch.sequence.missing_indices:
        summary_notes.append(
            "缺失帧序号：" + ", ".join(map(str, batch.sequence.missing_indices))
        )
    if batch.sequence.out_of_order_indices:
        summary_notes.append("帧序号在描述中非升序；导出顺序仍以描述数组为准。")
    for warning in batch.warnings:
        if warning["code"] in ("duplicate_indices", "missing_indices", "out_of_order"):
            continue
        summary_notes.append(warning["message"])

    generator: dict[str, Any] = {
        "name": "frame_export",
        "version": __version__,
        "resampling": batch.resampling,
    }
    if timestamp is not None:
        generator["generated_at"] = timestamp

    return {
        "schema_version": SCHEMA_VERSION,
        "generator": generator,
        "source": {
            "description": spec.display_path,
            "frame_count_declared": spec.declared_frame_count,
            "frame_count_entries": len(spec.frames),
        },
        "canvas": spec.canvas.as_dict(),
        "background": spec.background.as_dict(),
        "fps": _number(spec.fps),
        "fps_raw": spec.fps_raw if isinstance(spec.fps_raw, (int, float)) else str(spec.fps_raw),
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS[verdict],
        "verdict_reason": _verdict_reason(batch, verdict),
        "summary": {
            "frames_total": len(batch.frames),
            "frames_exported": len(succeeded),
            "frames_failed": len(failed),
            "frames_invalid": sum(1 for f in batch.frames if f.status == "invalid"),
            "frames_missing_file": sum(1 for f in batch.frames if f.status == "missing_file"),
            "frames_corrupt": sum(1 for f in batch.frames if f.status == "corrupt"),
            "duration_ms_exported": _number(batch.total_duration_ms),
            "duration_ms_all_entries": _number(
                sum(
                    float(f.spec.duration_ms)
                    for f in batch.frames
                    if f.spec.duration_valid
                )
            ),
            "failed_indices": [
                frame.spec.index
                if isinstance(frame.spec.index, (int, str, type(None)))
                else str(frame.spec.index)
                for frame in failed
            ],
        },
        "sequence": {
            "order_source": "description_array",
            "index_base": batch.sequence.index_base,
            "missing_indices": batch.sequence.missing_indices,
            "duplicate_indices": batch.sequence.duplicate_indices,
            "out_of_order": batch.sequence.out_of_order_indices,
            "declared_count_matches": batch.sequence.declared_count_matches,
        },
        "notes": summary_notes,
        "frames": entries,
    }


def build_report_json(batch: BatchResult, *, timestamp: str | None = None) -> str:
    return json.dumps(build_report(batch, timestamp=timestamp), ensure_ascii=False, indent=2) + "\n"


def _empty_report(
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None,
    description: str | None,
    timestamp: str | None,
) -> dict[str, Any]:
    generator: dict[str, Any] = {
        "name": "frame_export",
        "version": __version__,
    }
    if timestamp is not None:
        generator["generated_at"] = timestamp
    return {
        "schema_version": SCHEMA_VERSION,
        "generator": generator,
        "source": {"description": description},
        "canvas": None,
        "background": None,
        "fps": None,
        "verdict": VERDICT_UNEXPORTABLE,
        "verdict_label": VERDICT_LABELS[VERDICT_UNEXPORTABLE],
        "verdict_reason": f"{message}（错误代码 {code}）",
        "error": {"code": code, "message": message, "details": details or {}},
        "summary": {
            "frames_total": 0,
            "frames_exported": 0,
            "frames_failed": 0,
            "frames_invalid": 0,
            "frames_missing_file": 0,
            "frames_corrupt": 0,
            "duration_ms_exported": 0,
            "duration_ms_all_entries": 0,
            "failed_indices": [],
        },
        "sequence": {
            "order_source": "description_array",
            "missing_indices": [],
            "duplicate_indices": [],
            "out_of_order": False,
            "declared_count_matches": None,
        },
        "notes": [message],
        "frames": [],
    }


def build_empty_report_json(
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    description: str | None = None,
    timestamp: str | None = None,
) -> str:
    report = _empty_report(
        code=code,
        message=message,
        details=details,
        description=description,
        timestamp=timestamp,
    )
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


def _timeline_html(batch: BatchResult, total_duration: Any) -> str:
    total = float(total_duration or 0)
    if total <= 0:
        return '<p class="sub">所有已导出帧的停留时长均为 0 或无效，时间线为空。</p>'
    segments: list[str] = []
    cursor = 0.0
    for frame_position, result in enumerate(batch.succeeded, start=1):
        duration = float(result.spec.duration_ms)
        pct = duration / total * 100
        label = f"帧 {result.spec.index} · {_number(duration)} ms · {pct:.2f}%"
        segments.append(
            f'<div class="seg" style="width:{pct:.6f}%" '
            f'title="{html.escape(label)}" '
            f'data-frame="{html.escape(str(result.spec.index))}"></div>'
        )
        cursor += duration
    return (
        f'<div class="bar">{"".join(segments)}</div>'
        f'<div class="scale"><span>0 ms</span>'
        f'<span>累计 {_number(cursor)} ms</span></div>'
    )


def build_preview_html(batch: BatchResult, report: dict[str, Any]) -> str:
    spec = batch.spec
    verdict = report["verdict"]
    verdict_class = {
        VERDICT_COMPLETE: "ok",
        VERDICT_PARTIAL: "warn",
        VERDICT_UNEXPORTABLE: "bad",
    }[verdict]

    report_frames = report.get("frames", [])
    rows: list[str] = []
    timeline_cursor = 0.0
    for result in batch.frames:
        spec_frame = result.spec
        composited = result.composited
        entry = (
            report_frames[result.ordinal - 1]
            if result.ordinal - 1 < len(report_frames)
            else {}
        )
        index_text = html.escape(str(spec_frame.index))
        source_text = html.escape(str(spec_frame.file_ref))
        duration_text = html.escape(
            _duration_display(spec_frame.duration_ms if spec_frame.duration_valid else None)
        )
        data_duration = (
            f'{float(spec_frame.duration_ms)}'
            if spec_frame.duration_valid
            else ""
        )

        if result.succeeded and composited is not None:
            start = timeline_cursor
            timeline_cursor += float(spec_frame.duration_ms)
            placement = PLACEMENT_LABELS[composited.placement]
            size_text = (
                f"{composited.source_width}×{composited.source_height} → "
                f"{composited.image.width}×{composited.image.height}（{placement}）"
            )
            if composited.has_alpha and composited.alpha_min < 255:
                channel_text = f"带透明通道（alpha {composited.alpha_min}–{composited.alpha_max}）"
            elif composited.has_alpha:
                channel_text = "透明通道全部不透明"
            else:
                channel_text = "无透明通道（按不透明处理）"
            badge = '<span class="badge ok">已导出</span>'
            preview = (
                f'<img class="thumb" src="{html.escape(result.png_relpath or "")}" '
                f'width="{composited.image.width}" height="{composited.image.height}" '
                f'alt="帧 {index_text}" loading="lazy">'
            )
            meta_extra = (
                f"<li>时间线：{_number(start)} ms → {_number(timeline_cursor)} ms</li>"
            )
            css_class = "frame-card"
        else:
            badge = f'<span class="badge bad">{html.escape(STATUS_LABELS[result.status])}</span>'
            preview = (
                '<div class="missing" role="note">'
                f'<span>未完成：{html.escape(STATUS_LABELS[result.status])}</span>'
                "</div>"
            )
            size_text = "—"
            channel_text = "—"
            meta_extra = ""
            css_class = "frame-card failed"

        notes = "".join(
            f"<li>{html.escape(str(note))}</li>" for note in entry.get("notes", [])
        )
        error_line = ""
        if not result.succeeded and result.error_message:
            error_line = (
                f'<p class="error">原因：{html.escape(result.error_message)}</p>'
            )

        rows.append(
            "\n".join(
                [
                    f'<article class="{css_class}" data-index="{index_text}" '
                    f'data-ordinal="{result.ordinal}" '
                    f'data-duration-ms="{data_duration}" '
                    f'data-status="{result.status}">',
                    f"  <header><span class=\"seq\">位置 {result.ordinal}</span>"
                    f' <span class="idx">序号 {index_text}</span> {badge}</header>',
                    f'  <div class="preview">{preview}</div>',
                    error_line,
                    "  <dl>",
                    f"    <dt>源文件</dt><dd><code>{source_text}</code></dd>",
                    f"    <dt>停留</dt><dd>{duration_text}</dd>",
                    f"    <dt>尺寸</dt><dd>{html.escape(size_text)}</dd>",
                    f"    <dt>通道</dt><dd>{html.escape(channel_text)}</dd>",
                    "  </dl>",
                    f'  <ul class="meta">{meta_extra}{notes}</ul>',
                    "</article>",
                ]
            )
        )

    total_duration = report["summary"]["duration_ms_exported"]
    cards = "\n".join(rows) if rows else '<p class="empty">描述中没有任何帧条目。</p>'
    notes_html = "".join(
        f"<li>{html.escape(str(note))}</li>" for note in report.get("notes", [])
    )
    notes_block = (
        f'<ul class="notes">{notes_html}</ul>'
        if notes_html
        else '<p class="notes-empty">无额外说明。</p>'
    )

    failure_rows = ""
    if batch.failed:
        items = "".join(
            f"<tr><td>{result.ordinal}</td><td>{html.escape(str(result.spec.index))}</td>"
            f"<td><code>{html.escape(str(result.spec.file_ref))}</code></td>"
            f"<td>{html.escape(STATUS_LABELS[result.status])}</td>"
            f"<td>{html.escape(result.error_message or '')}</td></tr>"
            for result in batch.failed
        )
        failure_rows = (
            '<section class="failures"><h2>未完成帧</h2>'
            "<table><thead><tr><th>位置</th><th>序号</th><th>源文件</th>"
            f"<th>状态</th><th>原因</th></tr></thead><tbody>{items}</tbody></table></section>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>帧序列预览 — {html.escape(spec.display_path)}</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #14161a;
  --panel: #1e222a;
  --line: #313845;
  --text: #e7ecf3;
  --muted: #9aa6b8;
  --ok: #2f9e6e;
  --warn: #c58b2a;
  --bad: #c4474d;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text);
        font: 14px/1.5 "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; }}
header.top {{ padding: 24px 28px; border-bottom: 1px solid var(--line);
              background: var(--panel); }}
h1 {{ margin: 0 0 6px; font-size: 20px; }}
.sub {{ color: var(--muted); font-size: 13px; }}
.verdict {{ display: inline-block; margin-top: 10px; padding: 4px 12px;
            border-radius: 999px; font-weight: 600; }}
.verdict.ok {{ background: rgba(47,158,110,.18); color: #6fd3a3; border: 1px solid var(--ok); }}
.verdict.warn {{ background: rgba(197,139,42,.18); color: #e8b563; border: 1px solid var(--warn); }}
.verdict.bad {{ background: rgba(196,71,77,.18); color: #ef8b90; border: 1px solid var(--bad); }}
.stats {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }}
.stat {{ background: #262c36; border: 1px solid var(--line); border-radius: 8px;
         padding: 8px 14px; min-width: 120px; }}
.stat b {{ display: block; font-size: 18px; }}
.stat span {{ color: var(--muted); font-size: 12px; }}
main {{ padding: 22px 28px 60px; }}
.timeline {{ border: 1px solid var(--line); background: var(--panel);
             border-radius: 10px; padding: 12px 16px; margin-bottom: 18px; }}
.timeline h2, .failures h2 {{ font-size: 15px; margin: 0 0 8px; }}
.timeline .bar {{ display: flex; height: 22px; border-radius: 5px; overflow: hidden;
                 border: 1px solid var(--line); }}
.timeline .seg {{ height: 100%; background: #3f7fbf; }}
.timeline .seg:nth-child(2n) {{ filter: brightness(.75); }}
.timeline .scale {{ display: flex; justify-content: space-between;
                    color: var(--muted); font-size: 11px; margin-top: 4px; }}
.controls {{ display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap;
             align-items: center; }}
.controls button {{ background: #2a313c; color: var(--text); border: 1px solid var(--line);
                    border-radius: 7px; padding: 7px 14px; cursor: pointer; font: inherit; }}
.controls button:hover {{ background: #343d4b; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
         gap: 14px; }}
.frame-card {{ background: var(--panel); border: 1px solid var(--line);
               border-radius: 10px; padding: 12px; display: flex; flex-direction: column; }}
.frame-card.failed {{ border-color: var(--bad); }}
.frame-card header {{ display: flex; gap: 8px; align-items: center;
                      justify-content: space-between; margin-bottom: 8px; }}
.seq {{ font-weight: 700; }} .idx {{ color: var(--muted); font-size: 12px; }}
.badge {{ font-size: 11px; padding: 2px 8px; border-radius: 999px; }}
.badge.ok {{ background: rgba(47,158,110,.2); color: #6fd3a3; }}
.badge.bad {{ background: rgba(196,71,77,.2); color: #ef8b90; }}
.preview {{ background: repeating-conic-gradient(#2b3038 0 25%, #22262d 0 50%)
            0 0/16px 16px; border-radius: 6px; display: flex; align-items: center;
            justify-content: center; min-height: 140px; padding: 8px; }}
.thumb {{ max-width: 100%; height: auto; }}
.missing {{ width: 100%; min-height: 120px; display: flex; align-items: center;
            justify-content: center; color: #ef8b90; border: 1px dashed var(--bad);
            border-radius: 6px; }}
.error {{ color: #ef8b90; font-size: 12px; margin: 8px 0 0; }}
dl {{ display: grid; grid-template-columns: 54px 1fr; gap: 2px 8px; margin: 10px 0 0;
      font-size: 12px; }}
dt {{ color: var(--muted); }} dd {{ margin: 0; word-break: break-all; }}
ul.meta {{ margin: 8px 0 0; padding-left: 16px; color: var(--muted); font-size: 11px; }}
ul.notes {{ margin: 0; padding-left: 18px; }}
.failures {{ margin-top: 22px; border: 1px solid var(--bad); border-radius: 10px;
             background: var(--panel); padding: 14px 18px; }}
.failures table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
.failures th, .failures td {{ text-align: left; padding: 6px 8px;
                              border-bottom: 1px solid var(--line); }}
.failures th {{ color: var(--muted); font-weight: 600; }}
footer {{ color: var(--muted); font-size: 12px; margin-top: 28px; }}
</style>
</head>
<body>
<header class="top">
  <h1>帧序列预览</h1>
  <div class="sub">描述：<code>{html.escape(spec.display_path)}</code> ·
    画幅 {spec.canvas.width}×{spec.canvas.height} ·
    背景 {html.escape(spec.background.value)} ·
    {html.escape(str(_number(spec.fps)))} fps</div>
  <div class="verdict {verdict_class}">{html.escape(VERDICT_LABELS[verdict])}</div>
  <div class="stats">
    <div class="stat"><b>{report["summary"]["frames_total"]}</b><span>帧条目</span></div>
    <div class="stat"><b>{report["summary"]["frames_exported"]}</b><span>已导出</span></div>
    <div class="stat"><b>{report["summary"]["frames_failed"]}</b><span>未完成</span></div>
    <div class="stat"><b>{html.escape(str(total_duration))} ms</b><span>累计停留</span></div>
  </div>
</header>
<main>
  <div class="timeline">
    <h2>时间线（按描述顺序）</h2>
    {_timeline_html(batch, total_duration)}
  </div>
  <div class="controls">
    <button type="button" id="play">播放</button>
    <button type="button" id="pause">暂停</button>
    <button type="button" id="reset">回到第一帧</button>
    <span class="sub" id="clock">当前帧：第 1 帧 / 共 {len(batch.frames)} 帧</span>
  </div>
  <div class="grid" id="grid">
{cards}
  </div>
  <section style="margin-top:22px">
    <h2 style="font-size:15px">整批核对说明</h2>
    {notes_block}
  </section>
  {failure_rows}
  <footer>由 frame_export 生成；本页离线可用，无需网络。</footer>
</main>
<script>
(function () {{
  const cards = Array.from(document.querySelectorAll('.frame-card'));
  const clock = document.getElementById('clock');
  const playBtn = document.getElementById('play');
  const pauseBtn = document.getElementById('pause');
  const resetBtn = document.getElementById('reset');
  let timer = null;
  let cursor = 0;

  function focusCard(i) {{
    if (i < 0 || i >= cards.length) return false;
    cards[i].scrollIntoView({{ block: 'nearest', behavior: 'smooth' }});
    cards[i].style.outline = '2px solid #6fd3a3';
    setTimeout(function () {{ cards[i].style.outline = ''; }}, 900);
    if (clock) clock.textContent = '当前帧：第 ' + (i + 1) + ' 帧 / 共 ' + cards.length + ' 帧';
    return true;
  }}
  function stop() {{ if (timer) {{ clearTimeout(timer); timer = null; }} }}
  function next() {{
    if (cursor >= cards.length) {{ stop(); return; }}
    focusCard(cursor);
    const raw = cards[cursor].getAttribute('data-duration-ms');
    let ms = 1000;
    if (raw !== null && raw !== '') {{
      const parsed = Number(raw);
      if (Number.isFinite(parsed) && parsed >= 0) ms = parsed;
    }}
    cursor += 1;
    timer = setTimeout(next, Math.max(0, ms));
  }}
  if (playBtn) playBtn.addEventListener('click', function () {{
    stop();
    if (cursor >= cards.length) cursor = 0;
    next();
  }});
  if (pauseBtn) pauseBtn.addEventListener('click', stop);
  if (resetBtn) resetBtn.addEventListener('click', function () {{
    stop();
    cursor = 0;
    focusCard(0);
  }});
  document.addEventListener('keydown', function (event) {{
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {{
      stop();
      const step = event.key === 'ArrowRight' ? 1 : -1;
      const current = Number((clock && /第 (\\d+) 帧/.exec(clock.textContent) || [0, 1])[1]) - 1;
      cursor = Math.min(cards.length - 1, Math.max(0, current + step));
      focusCard(cursor);
      event.preventDefault();
    }} else if (event.key === ' ') {{
      stop();
      if (cursor >= cards.length) cursor = 0;
      next();
      event.preventDefault();
    }}
  }});
}})();
</script>
</body>
</html>
"""
