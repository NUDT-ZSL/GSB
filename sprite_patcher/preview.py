"""自包含对比预览 HTML 生成。

功能：
- 原图与新版并排显示（可切换为单图 A/B 对比模式）；
- 用 SVG 叠加网格格线，并标出右侧/底部边带；
- 差异列表按变化像素降序，点击某行时两侧预览同时定位到该格并高亮边框；
- 支持 1x/2x/4x 缩放；图片以 base64 内嵌，无任何外部资源依赖。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import quote

from PIL import Image

from .config import Config
from .geometry import Geometry
from .report import CellDiff


def _b64_png(image: Image.Image) -> str:
    import io

    buf = io.BytesIO()
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _grid_svg(width: int, height: int, geometry: Geometry,
              grid: tuple[int, int], prefix: str) -> str:
    rows, cols = grid
    parts = [
        f'<svg class="grid-lines" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
    ]
    for c in range(cols + 1):
        x = min(c * geometry.cell_w, width)
        parts.append(
            f'<line x1="{x}" y1="0" x2="{x}" y2="{height}" '
            f'stroke="#2563eb" stroke-width="1" opacity="0.55"/>'
        )
    for r in range(rows + 1):
        y = min(r * geometry.cell_h, height)
        parts.append(
            f'<line x1="0" y1="{y}" x2="{width}" y2="{y}" '
            f'stroke="#2563eb" stroke-width="1" opacity="0.55"/>'
        )
    # 边带区域用半透明琥珀色标出
    if geometry.right_band.pixels:
        x0 = width - geometry.right_band.pixels
        parts.append(
            f'<rect x="{x0}" y="0" width="{geometry.right_band.pixels}" '
            f'height="{height}" fill="#f59e0b" opacity="0.28"/>'
        )
    if geometry.bottom_band.pixels:
        y0 = height - geometry.bottom_band.pixels
        parts.append(
            f'<rect x="0" y="{y0}" width="{width}" '
            f'height="{geometry.bottom_band.pixels}" fill="#f59e0b" '
            f'opacity="0.28"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def build_preview_html(
    old: Image.Image,
    new: Image.Image,
    config: Config,
    geometry: Geometry,
    diffs: list[CellDiff],
    report: dict,
    output_path: str | Path,
) -> None:
    old_b64 = _b64_png(old)
    new_b64 = _b64_png(new)
    w, h = config.sheet.width, config.sheet.height

    cells_data = [
        {
            "index": d.index, "row": d.row, "column": d.column,
            "changed": d.changed_pixels,
            "verdict": "已改动" if d.changed else "未改动",
        }
        for d in diffs
    ]
    boxes = {
        d.index: geometry.cell_box(d.row, d.column) for d in diffs
    }
    meta = {
        "width": w, "height": h,
        "cellW": geometry.cell_w, "cellH": geometry.cell_h,
        "rows": config.grid.rows, "cols": config.grid.columns,
        "boxes": {str(k): v for k, v in boxes.items()},
        "cells": cells_data,
        "noSubstantial": report["summary"]["no_substantial_change"],
    }

    old_svg = _grid_svg(w, h, geometry,
                        (config.grid.rows, config.grid.columns), "old")
    new_svg = _grid_svg(w, h, geometry,
                        (config.grid.rows, config.grid.columns), "new")

    # URI 编码后内嵌：输出仅含 ASCII 安全字符（无引号、反斜杠、<），
    # 浏览器侧 JSON.parse(decodeURIComponent(...)) 可无损还原，规避所有转义层级问题。
    safe = lambda s: quote(s, safe="")
    doc = _TEMPLATE.replace("__OLD_B64__", old_b64).replace("__NEW_B64__", new_b64)
    doc = doc.replace("__OLD_SVG__", old_svg).replace("__NEW_SVG__", new_svg)
    doc = doc.replace("__WIDTH__", str(w)).replace("__HEIGHT__", str(h))
    doc = doc.replace("__META__", safe(json.dumps(meta, ensure_ascii=False)))
    doc = doc.replace("__SUMMARY__", safe(json.dumps(report["summary"], ensure_ascii=False)))
    doc = doc.replace("__BANDS__", safe(json.dumps(report["edge_bands"], ensure_ascii=False)))
    Path(output_path).write_text(doc, encoding="utf-8")


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>精灵表补丁对比预览</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #0f172a; color: #e2e8f0; }
  header { padding: 14px 20px; background: #1e293b; border-bottom: 1px solid #334155; }
  header h1 { margin: 0 0 6px; font-size: 18px; }
  .summary { font-size: 13px; color: #94a3b8; }
  .toolbar { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; }
  button { background: #334155; color: #e2e8f0; border: 1px solid #475569;
           border-radius: 6px; padding: 5px 12px; cursor: pointer; font-size: 13px; }
  button:hover { background: #475569; }
  button.active { background: #2563eb; border-color: #3b82f6; }
  .layout { display: flex; min-height: calc(100vh - 120px); }
  .panel-list { width: 320px; flex: 0 0 320px; border-right: 1px solid #334155;
                overflow: auto; background: #1e293b; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th, td { padding: 7px 9px; text-align: left; border-bottom: 1px solid #334155; }
  th { position: sticky; top: 0; background: #334155; z-index: 1; }
  tr.diff-row { cursor: pointer; }
  tr.diff-row:hover { background: #334155; }
  tr.diff-row.selected { background: #1d4ed8; }
  .badge { padding: 1px 7px; border-radius: 10px; font-size: 11px; }
  .badge.changed { background: #b45309; }
  .badge.same { background: #374151; color: #9ca3af; }
  .panel-view { flex: 1; overflow: auto; padding: 16px; }
  .panes { display: flex; gap: 18px; align-items: flex-start; }
  .pane { background: #1e293b; border: 1px solid #334155; border-radius: 8px;
          padding: 10px; flex: 0 0 auto; }
  .pane h3 { margin: 0 0 8px; font-size: 13px; color: #94a3b8; }
  .stage { position: relative; overflow: auto; width: 480px; height: 420px;
           background:
           conic-gradient(#475569 25%, #334155 0 50%, #475569 0 75%, #334155 0)
           0/20px 20px; border-radius: 4px; }
  .scaler { position: absolute; top: 0; left: 0; transform-origin: top left; }
  .scaler img, .scaler svg, .scaler .hl-box { position: absolute; left: 0; top: 0; }
  .grid-lines { pointer-events: none; }
  .hl-box { border: 3px solid #ef4444; box-shadow: 0 0 0 2px rgba(239,68,68,.35);
            display: none; pointer-events: none; z-index: 5; }
  body.ab-mode .pane-old { display: none; }
  body.ab-mode .stage { width: 640px; height: 520px; }
  #abToggle .ab-label-new, body.show-old .ab-label-old { display: inline; }
  #abToggle .ab-label-old, body.show-old .ab-label-new { display: none; }
  body.show-old .pane-new .scaler { visibility: hidden; }
  body.ab-mode.show-old .pane-new { display: block; }
  .coord-tip { font-size: 12px; color: #fbbf24; margin-top: 6px; min-height: 16px; }
  .note { font-size: 12px; color: #f59e0b; }
</style>
</head>
<body>
<header>
  <h1>精灵表补丁对比预览</h1>
  <div class="summary" id="summary"></div>
  <div class="summary" id="bands"></div>
  <div class="toolbar">
    <button id="modeSide" class="active">并排对比</button>
    <button id="modeAb">A/B 单图切换</button>
    <button id="abToggle" style="display:none"><span class="ab-label-new">当前：新版（点我切到原图）</span><span class="ab-label-old">当前：原图（点我切到新版）</span></button>
    <span style="align-self:center">缩放：</span>
    <button class="zoom" data-z="1">1x</button>
    <button class="zoom active" data-z="2">2x</button>
    <button class="zoom" data-z="4">4x</button>
  </div>
</header>
<div class="layout">
  <div class="panel-list">
    <table>
      <thead><tr><th>#</th><th>行,列</th><th>变化像素</th><th>判定</th></tr></thead>
      <tbody id="diffBody"></tbody>
    </table>
  </div>
  <div class="panel-view">
    <div class="panes">
      <div class="pane pane-old">
        <h3>原图（补丁前）</h3>
        <div class="stage" id="stageOld">
          <div class="scaler" id="scalerOld">
            <img alt="old" src="data:image/png;base64,__OLD_B64__" width="__WIDTH__" height="__HEIGHT__">
            __OLD_SVG__
            <div class="hl-box" id="hlOld"></div>
          </div>
        </div>
        <div class="coord-tip" id="tipOld"></div>
      </div>
      <div class="pane pane-new">
        <h3>新版（补丁后）</h3>
        <div class="stage" id="stageNew">
          <div class="scaler" id="scalerNew">
            <img alt="new" src="data:image/png;base64,__NEW_B64__" width="__WIDTH__" height="__HEIGHT__">
            __NEW_SVG__
            <div class="hl-box" id="hlNew"></div>
          </div>
        </div>
        <div class="coord-tip" id="tipNew"></div>
      </div>
    </div>
    <p class="note">琥珀色区域为不可整除产生的边带，保持原样；点击左侧差异列表任意行即可在两侧同时定位并高亮该格。</p>
  </div>
</div>
<script>
const META = JSON.parse(decodeURIComponent("__META__"));
const SUMMARY = JSON.parse(decodeURIComponent("__SUMMARY__"));
const BANDS = JSON.parse(decodeURIComponent("__BANDS__"));
let zoom = 2, selected = null;

document.getElementById("summary").textContent =
  "补丁 " + SUMMARY.patch_count + " 个（有效 " + SUMMARY.applied_patch_count +
  "，不可用 " + SUMMARY.unavailable_patch_count + "）；变化格 " +
  SUMMARY.changed_cell_count + "；变化像素 " + SUMMARY.total_changed_pixels +
  (SUMMARY.no_substantial_change ? "　⚠ " + SUMMARY.note : "");
document.getElementById("bands").textContent =
  "边带：右侧 " + BANDS.right.pixels + " 像素，底部 " + BANDS.bottom.pixels +
  " 像素。" + BANDS.divisibility_hint;

const body = document.getElementById("diffBody");
META.cells.forEach(c => {
  const tr = document.createElement("tr");
  tr.className = "diff-row";
  tr.dataset.index = c.index;
  tr.innerHTML = "<td>" + c.index + "</td><td>(" + c.row + "," + c.column +
    ")</td><td>" + c.changed + "</td><td><span class='badge " +
    (c.changed ? "changed" : "same") + "'>" + c.verdict + "</span></td>";
  tr.addEventListener("click", () => selectCell(c.index));
  body.appendChild(tr);
});

function applyZoom() {
  document.querySelectorAll(".scaler").forEach(s => {
    s.style.transform = "scale(" + zoom + ")";
    s.style.width = (META.width * zoom) + "px";
    s.style.height = (META.height * zoom) + "px";
  });
  document.querySelectorAll(".zoom").forEach(b =>
    b.classList.toggle("active", Number(b.dataset.z) === zoom));
  if (selected !== null) positionHighlight(selected);
}
document.querySelectorAll(".zoom").forEach(b =>
  b.addEventListener("click", () => { zoom = Number(b.dataset.z); applyZoom(); }));

document.getElementById("modeSide").addEventListener("click", () => {
  document.body.classList.remove("ab-mode", "show-old");
  document.getElementById("abToggle").style.display = "none";
  document.getElementById("modeSide").classList.add("active");
  document.getElementById("modeAb").classList.remove("active");
});
document.getElementById("modeAb").addEventListener("click", () => {
  document.body.classList.add("ab-mode");
  document.getElementById("abToggle").style.display = "";
  document.getElementById("modeAb").classList.add("active");
  document.getElementById("modeSide").classList.remove("active");
});
document.getElementById("abToggle").addEventListener("click", () =>
  document.body.classList.toggle("show-old"));

function selectCell(index) {
  selected = index;
  document.querySelectorAll("tr.diff-row").forEach(tr =>
    tr.classList.toggle("selected", Number(tr.dataset.index) === index));
  positionHighlight(index);
}

function positionHighlight(index) {
  const [l, t, r, b] = META.boxes[index];
  const cw = r - l, ch = b - t;
  [["hlOld", "stageOld", "tipOld"], ["hlNew", "stageNew", "tipNew"]]
    .forEach(([hlId, stageId, tipId]) => {
      const hl = document.getElementById(hlId);
      hl.style.display = "block";
      hl.style.left = l + "px"; hl.style.top = t + "px";
      hl.style.width = cw + "px"; hl.style.height = ch + "px";
      const stage = document.getElementById(stageId);
      stage.scrollTo({
        left: Math.max(0, l * zoom - stage.clientWidth / 2 + cw * zoom / 2),
        top: Math.max(0, t * zoom - stage.clientHeight / 2 + ch * zoom / 2),
        behavior: "smooth"
      });
      const cell = META.cells.find(x => x.index === index);
      document.getElementById(tipId).textContent =
        "定位到格子 #" + index + "（行 " + cell.row + "，列 " + cell.column +
        "），变化像素 " + cell.changed;
    });
}
applyZoom();
</script>
</body>
</html>
"""
