"""Offline diagnostic viewer over persisted render results."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from .errors import BusinessError
from .util import atomic_write_text, read_json


def _load_persisted(output_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    output_root = Path(output_root).resolve()
    report_path = output_root / "render_report.json"
    report = read_json(report_path, "E_VIEW")
    cameras: list[dict[str, Any]] = []
    for camera_report in report.get("cameras", []):
        outputs = camera_report.get("outputs", {})
        block_path = Path(outputs.get("blocks", ""))
        gray_path = Path(outputs.get("grayscale", ""))
        preview_path = Path(outputs.get("depth_preview", ""))
        if not block_path.is_file() or not gray_path.is_file() or not preview_path.is_file():
            raise BusinessError(
                "E_VIEW",
                "persisted diagnostic artifacts are incomplete",
                details={
                    "camera_id": camera_report.get("camera_id"),
                    "blocks": str(block_path),
                    "grayscale": str(gray_path),
                    "depth_preview": str(preview_path),
                },
            )
        blocks_document = read_json(block_path, "E_VIEW")
        try:
            grayscale_data = gray_path.read_bytes()
            depth_data = preview_path.read_bytes()
        except OSError as exc:
            raise BusinessError(
                "E_VIEW",
                "cannot read persisted diagnostic image",
                details={"camera_id": camera_report.get("camera_id"), "os_error": str(exc)},
            ) from exc
        cameras.append(
            {
                "report": camera_report,
                "blocks_document": blocks_document,
                "grayscale_data": grayscale_data,
                "depth_data": depth_data,
            }
        )
    return report, cameras


def build_static_viewer(output_root: Path) -> Path:
    """Build a single-file interactive HTML viewer with no external requests."""

    output_root = Path(output_root).resolve()
    report, cameras = _load_persisted(output_root)
    payload = {
        "task_id": report.get("task_id"),
        "cache_key": report.get("cache_key"),
        "dataset_digest": report.get("dataset_digest"),
        "edit": report.get("edit"),
        "cameras": [],
    }
    for camera in cameras:
        camera_report = camera["report"]
        blocks_document = camera["blocks_document"]
        payload["cameras"].append(
            {
                "camera_id": camera_report.get("camera_id"),
                "width": blocks_document.get("width"),
                "height": blocks_document.get("height"),
                "version_id": blocks_document.get("version_id"),
                "grayscale": "data:image/png;base64,"
                + base64.b64encode(camera["grayscale_data"]).decode("ascii"),
                "depth_preview": "data:image/png;base64,"
                + base64.b64encode(camera["depth_data"]).decode("ascii"),
                "blocks": blocks_document.get("blocks", []),
            }
        )
    data_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).replace(
        "</",
        "<\\/",
    )
    html = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Point Cloud Block Workbench</title>
<style>
:root { color-scheme: dark; --bg:#11151c; --panel:#1a2130; --line:#42506a; --text:#e8edf7; --muted:#9aabc7; --bad:#ff5666; --ok:#47d18c; }
* { box-sizing: border-box; }
body { margin:0; font-family:"Segoe UI",system-ui,sans-serif; background:var(--bg); color:var(--text); }
header { padding:16px 22px; border-bottom:1px solid var(--line); display:flex; gap:20px; align-items:baseline; flex-wrap:wrap; }
header h1 { font-size:18px; margin:0; letter-spacing:.02em; }
header .meta { color:var(--muted); font-size:12px; }
nav { display:flex; gap:8px; padding:12px 22px; border-bottom:1px solid var(--line); flex-wrap:wrap; }
button { background:#222c3d; color:var(--text); border:1px solid var(--line); border-radius:6px; padding:7px 12px; cursor:pointer; }
button.active { border-color:#6da8ff; background:#243858; }
main { display:grid; grid-template-columns:minmax(0,1fr) 390px; gap:16px; padding:18px 22px 28px; }
.views { display:grid; grid-template-columns:repeat(auto-fit,minmax(310px,1fr)); gap:16px; align-content:start; }
.view { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px; min-width:0; }
.view h2 { font-size:13px; font-weight:600; margin:0 0 9px; color:var(--muted); }
.image-wrap { position:relative; width:100%; background:#000; line-height:0; }
.image-wrap img { width:100%; height:auto; image-rendering:pixelated; display:block; }
.overlay { position:absolute; inset:0; }
.block { position:absolute; border:1px solid rgba(150,185,245,.48); cursor:crosshair; }
.block:hover { background:rgba(109,168,255,.16); }
.block.invalid { border:2px solid var(--bad); background:rgba(255,86,102,.12); }
.block.selected { outline:2px solid #ffe26a; outline-offset:-2px; }
aside { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; min-width:0; }
aside h2 { margin:0 0 12px; font-size:15px; }
.kv { display:grid; grid-template-columns:118px minmax(0,1fr); gap:5px 8px; font-size:12px; margin-bottom:14px; }
.kv .k { color:var(--muted); }
.kv .v { overflow-wrap:anywhere; }
.reason { border-left:3px solid var(--bad); padding:7px 9px; margin:7px 0; background:#261c25; font-size:12px; }
.reason code { color:#ffb3bb; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; background:#0d1219; border:1px solid #283247; padding:9px; max-height:190px; overflow:auto; font-size:11px; border-radius:5px; }
.ok { color:var(--ok); } .bad { color:var(--bad); } .muted { color:var(--muted); }
@media (max-width: 900px) { main { grid-template-columns:1fr; } aside { order:-1; } }
</style>
</head>
<body>
<header>
  <h1>离线点云分块诊断台</h1>
  <span class="meta" id="task-meta"></span>
</header>
<nav id="tabs"></nav>
<main>
  <section class="views">
    <div class="view">
      <h2>灰度投影 / 分块网格 / 失效高亮</h2>
      <div class="image-wrap" id="gray-wrap"><img id="gray-img" alt="grayscale projection"><div class="overlay" id="gray-overlay"></div></div>
    </div>
    <div class="view">
      <h2>世界单位深度预览 / 分块网格 / 失效高亮</h2>
      <div class="image-wrap" id="depth-wrap"><img id="depth-img" alt="depth preview"><div class="overlay" id="depth-overlay"></div></div>
    </div>
  </section>
  <aside>
    <h2 id="detail-title">选择一个分块</h2>
    <div class="kv" id="detail-kv"></div>
    <div id="reasons"></div>
    <div class="muted" style="font-size:12px;margin-top:10px">受影响点</div>
    <pre id="affected"></pre>
    <div class="muted" style="font-size:12px;margin-top:10px">依赖点集</div>
    <pre id="dependencies"></pre>
    <div class="muted" style="font-size:12px;margin-top:10px">摘要校验</div>
    <pre id="digest"></pre>
  </aside>
</main>
<script>
const DATA = __DATA__;
let activeCamera = 0;
let activeBlock = null;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
}

function renderTabs() {
  const tabs = document.getElementById("tabs");
  tabs.innerHTML = "";
  DATA.cameras.forEach((camera, index) => {
    const button = document.createElement("button");
    button.textContent = camera.camera_id + (camera.blocks.some(b => b.status === "render" || b.status === "digest_repair") ? " · 有补渲" : " · 全部复用");
    button.className = index === activeCamera ? "active" : "";
    button.onclick = () => { activeCamera = index; activeBlock = null; renderCamera(); };
    tabs.appendChild(button);
  });
}

function renderOverlay(overlay, camera) {
  overlay.innerHTML = "";
  camera.blocks.forEach(block => {
    const rect = block.rect;
    const node = document.createElement("div");
    const invalid = block.status === "render" || block.status === "digest_repair";
    node.className = "block" + (invalid ? " invalid" : "") + (activeBlock === block.index ? " selected" : "");
    node.style.left = (100 * rect.x0 / camera.width) + "%";
    node.style.top = (100 * rect.y0 / camera.height) + "%";
    node.style.width = (100 * (rect.x1 - rect.x0) / camera.width) + "%";
    node.style.height = (100 * (rect.y1 - rect.y0) / camera.height) + "%";
    node.title = block.block_id + " [" + block.status + "]";
    node.onclick = () => { activeBlock = block.index; showBlock(); renderCamera(); };
    overlay.appendChild(node);
  });
}

function renderCamera() {
  const camera = DATA.cameras[activeCamera];
  if (!camera) return;
  renderTabs();
  document.getElementById("task-meta").textContent =
    "task=" + DATA.task_id + "  cache=" + DATA.cache_key + "  dataset=" + String(DATA.dataset_digest).slice(0, 16) + "…  camera=" + camera.camera_id;
  document.getElementById("gray-img").src = camera.grayscale;
  document.getElementById("depth-img").src = camera.depth_preview;
  renderOverlay(document.getElementById("gray-overlay"), camera);
  renderOverlay(document.getElementById("depth-overlay"), camera);
  if (activeBlock === null && camera.blocks.length) {
    const invalid = camera.blocks.find(block => block.status === "render" || block.status === "digest_repair");
    activeBlock = (invalid || camera.blocks[0]).index;
  }
  showBlock();
}

function showBlock() {
  const camera = DATA.cameras[activeCamera];
  const block = camera && camera.blocks.find(item => item.index === activeBlock);
  if (!block) return;
  const invalid = block.status === "render" || block.status === "digest_repair";
  document.getElementById("detail-title").innerHTML =
    esc(block.block_id) + " <span class='" + (invalid ? "bad" : "ok") + "'>[" + esc(block.status) + "]</span>";
  const rect = block.rect;
  document.getElementById("detail-kv").innerHTML =
    "<span class='k'>屏幕区域</span><span class='v'>x " + rect.x0 + "…" + rect.x1 + " / y " + rect.y0 + "…" + rect.y1 + "</span>" +
    "<span class='k'>输出像素</span><span class='v'>" + (rect.x1 - rect.x0) + " × " + (rect.y1 - rect.y0) + "</span>" +
    "<span class='k'>受影响点数</span><span class='v'>" + block.affected_point_count + "</span>" +
    "<span class='k'>依赖点数</span><span class='v'>" + block.dependency_count + "</span>" +
    "<span class='k'>世界深度范围</span><span class='v'>" +
      ((Number(block.depth_summary.min_world).toFixed(4) + " … " + Number(block.depth_summary.max_world).toFixed(4)) +
       "（有效 " + block.depth_summary.finite_pixels + " px）" if block.depth_summary && block.depth_summary.finite_pixels else "无有限深度") +
    "</span>" +
    "<span class='k'>版本</span><span class='v'>" + esc(String(block.version_id).slice(0, 20)) + "…</span>";
  const reasons = document.getElementById("reasons");
  reasons.innerHTML = "";
  (block.reasons || []).forEach(reason => {
    const node = document.createElement("div");
    node.className = "reason";
    node.innerHTML = "<code>" + esc(reason.code) + "</code><br>" + esc(reason.message) +
      (reason.edit_ids && reason.edit_ids.length ? "<br>编辑：" + esc(reason.edit_ids.join(", ")) : "");
    reasons.appendChild(node);
  });
  document.getElementById("affected").textContent =
    (block.affected_points || []).length ? block.affected_points.join(", ") : "（无）";
  document.getElementById("dependencies").textContent =
    (block.dependency_indices || []).length ? block.dependency_indices.join(", ") : "（无）";
  document.getElementById("digest").innerHTML =
    (block.digest_ok ? "<span class='ok'>OK</span>" : "<span class='bad'>MISMATCH</span>") +
    "\\ncontent  " + esc(block.content_digest) +
    "\\ndeps     " + esc(block.dependency_digest);
}

renderCamera();
</script>
</body>
</html>
"""
    html = html.replace("__DATA__", data_json)
    target = output_root / "workbench.html"
    try:
        atomic_write_text(target, html)
    except OSError as exc:
        raise BusinessError(
            "E_VIEW",
            "cannot write offline diagnostic viewer",
            details={"path": str(target), "os_error": str(exc)},
        ) from exc
    return target


def launch_tk_viewer(
    output_root: Path,
    initial_camera: str | None = None,
    *,
    auto_close_ms: int | None = None,
) -> None:
    """Launch a local Tk inspector that reads only persisted results."""

    import tkinter as tk
    from tkinter import ttk

    output_root = Path(output_root).resolve()
    report, camera_data = _load_persisted(output_root)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise BusinessError(
            "E_VIEW",
            "Tk display is unavailable; regenerate or open workbench.html instead",
            details={"os_error": str(exc)},
        ) from exc
    root.title("Point Cloud Block Workbench - persisted results")
    root.configure(bg="#11151c")
    outer = ttk.Frame(root)
    outer.pack(fill="both", expand=True)
    notebook = ttk.Notebook(outer)
    notebook.pack(side="left", fill="both", expand=True)
    details = tk.Text(
        outer,
        width=52,
        background="#1a2130",
        foreground="#e8edf7",
        insertbackground="#e8edf7",
        relief="flat",
        wrap="word",
    )
    details.pack(side="right", fill="y")
    images: list[Any] = []
    scale = 4

    for camera_entry in camera_data:
        camera_report = camera_entry["report"]
        blocks_document = camera_entry["blocks_document"]
        tab = ttk.Frame(notebook)
        notebook.add(tab, text=str(camera_report.get("camera_id")))
        gray_image = tk.PhotoImage(file=str(Path(camera_report["outputs"]["grayscale"]))).zoom(scale, scale)
        depth_image = tk.PhotoImage(
            file=str(Path(camera_report["outputs"]["depth_preview"]))
        ).zoom(scale, scale)
        images.extend([gray_image, depth_image])
        width = int(blocks_document["width"]) * scale
        height = int(blocks_document["height"]) * scale
        canvas = tk.Canvas(tab, width=width * 2 + 12, height=height, bg="#000000", highlightthickness=0)
        canvas.pack()
        canvas.create_image(0, 0, anchor="nw", image=gray_image)
        canvas.create_image(width + 12, 0, anchor="nw", image=depth_image)
        blocks = blocks_document.get("blocks", [])
        for offset in (0, width + 12):
            for block in blocks:
                rect = block["rect"]
                invalid = block["status"] in {"render", "digest_repair"}
                canvas.create_rectangle(
                    offset + rect["x0"] * scale,
                    rect["y0"] * scale,
                    offset + rect["x1"] * scale,
                    rect["y1"] * scale,
                    outline="#ff5666" if invalid else "#5f789f",
                    width=2 if invalid else 1,
                )

        def select_block(event: Any, *, blocks=blocks, width=width) -> None:
            image_x = int(event.x)
            if image_x >= width + 12:
                image_x -= width + 12
            image_y = int(event.y)
            if image_x < 0 or image_y < 0:
                return
            for block in blocks:
                rect = block["rect"]
                if rect["x0"] * scale <= image_x < rect["x1"] * scale and rect["y0"] * scale <= image_y < rect["y1"] * scale:
                    details.delete("1.0", "end")
                    details.insert(
                        "end",
                        f"{block['block_id']} [{block['status']}]\n"
                        f"screen: x {rect['x0']}..{rect['x1']}, y {rect['y0']}..{rect['y1']}\n"
                        f"affected points: {block['affected_point_count']}\n"
                        f"dependencies: {block['dependency_count']}\n"
                        f"depth world range: {block.get('depth_summary', {}).get('min_world')} .. "
                        f"{block.get('depth_summary', {}).get('max_world')} "
                        f"({block.get('depth_summary', {}).get('finite_pixels', 0)} px)\n"
                        f"digest ok: {block['digest_ok']}\n\nreasons:\n",
                    )
                    for reason in block.get("reasons", []):
                        details.insert(
                            "end",
                            f"- {reason.get('code')}: {reason.get('message')}\n",
                        )
                    details.insert(
                        "end",
                        "\naffected point indices:\n"
                        + (", ".join(str(value) for value in block.get("affected_points", [])) or "(none)")
                        + "\n\ndependency point indices:\n"
                        + (", ".join(str(value) for value in block.get("dependency_indices", [])) or "(none)")
                        + "\n\ncontent digest:\n"
                        + str(block.get("content_digest"))
                        + "\n\ndependency digest:\n"
                        + str(block.get("dependency_digest"))
                        + "\n",
                    )
                    return

        canvas.bind("<Button-1>", select_block)
    if initial_camera:
        for index in range(len(camera_data)):
            if camera_data[index]["report"].get("camera_id") == initial_camera:
                notebook.select(index)
                break
    details.insert("end", "Click a block in either image to inspect invalidation reasons and dependencies.\n")
    if auto_close_ms is not None:
        root.after(max(1, int(auto_close_ms)), root.destroy)
    root.mainloop()

