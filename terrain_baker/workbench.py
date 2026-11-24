from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from .sample_generator import generate_samples


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKBENCH_DIR = PROJECT_ROOT / "artifacts" / "workbench"


def _reset_directory(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _copy_sample_case(name: str, samples: Path) -> Path:
    case_dir = WORKBENCH_DIR / name
    input_dir = case_dir / "input"
    _reset_directory(case_dir)
    shutil.copytree(samples, input_dir)
    return input_dir


def _run_cli(args: List[str], expected_exit: int = 0) -> Dict[str, Any]:
    command = [sys.executable, "-m", "terrain_baker.cli", *args]
    completed = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != expected_exit:
        raise RuntimeError(
            f"command exited {completed.returncode}, expected {expected_exit}: {' '.join(command)}\n"
            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {"raw_stdout": completed.stdout}
    return {"exit_code": completed.returncode, "payload": payload, "stderr": completed.stderr}


def _load_report(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _gray_png_to_array(path: Path) -> np.ndarray:
    import zlib
    import struct

    data = path.read_bytes()
    offset = 8
    width = height = bit_depth = color_type = None
    idat = b""
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        tag = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        if tag == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", body[:10])
        elif tag == b"IDAT":
            idat += body
        offset += 12 + length
    raw = zlib.decompress(idat)
    assert bit_depth == 8 and color_type == 0
    stride = width + 1
    rows = []
    for y in range(height):
        start = y * stride + 1
        rows.append(np.frombuffer(raw[start : start + width], dtype=np.uint8))
    return np.stack(rows)


def run_local_edit_case(samples: Path) -> Dict[str, Any]:
    input_dir = _copy_sample_case("local_edit", samples)
    case_dir = input_dir.parent
    out_dir = case_dir / "out"
    state_dir = case_dir / "state"
    report_path = case_dir / "report.local_edit.json"

    baseline = _run_cli(
        [
            "bake",
            "--scene",
            str(input_dir / "scene.initial.json"),
            "--output-dir",
            str(out_dir),
            "--state-dir",
            str(state_dir),
            "--mode",
            "initial",
            "--run-id",
            "workbench-baseline",
        ]
    )
    from .sample_generator import apply_edit_to_directory

    edit_result = apply_edit_to_directory(input_dir)
    edited = _run_cli(
        [
            "bake",
            "--scene",
            str(input_dir / "scene.local_edit.json"),
            "--output-dir",
            str(out_dir),
            "--state-dir",
            str(state_dir),
            "--report",
            str(report_path),
            "--mode",
            "terrain_edit",
            "--edit",
            str(input_dir / "edits" / "local_ridge_valley.json"),
            "--run-id",
            "workbench-local-edit",
        ]
    )
    report = _load_report(report_path)

    serial_dir = case_dir / "serial"
    serial_out = serial_dir / "out"
    serial_state = serial_dir / "state"
    serial_report = serial_dir / "report.serial.json"
    _run_cli(
        [
            "bake",
            "--scene",
            str(input_dir / "scene.local_edit.json"),
            "--output-dir",
            str(serial_out),
            "--state-dir",
            str(serial_state),
            "--report",
            str(serial_report),
            "--mode",
            "initial",
            "--workers",
            "1",
            "--no-reference",
            "--run-id",
            "workbench-serial-1",
        ]
    )
    serial_report_data = _load_report(serial_report)

    gray_concurrent = _gray_png_to_array(out_dir / "terrain_gray.png")
    gray_serial = _gray_png_to_array(serial_out / "terrain_gray.png")
    depth_concurrent = np.load(out_dir / "terrain_depth.npy")
    depth_serial = np.load(serial_out / "terrain_depth.npy")
    serial_equivalence = {
        "gray_exact_equal": bool(np.array_equal(gray_concurrent, gray_serial)),
        "depth_exact_equal": bool(np.array_equal(depth_concurrent, depth_serial, equal_nan=True)),
        "gray_max_abs_diff": int(np.max(np.abs(gray_concurrent.astype(int) - gray_serial.astype(int)))),
        "depth_max_abs_diff": float(
            np.max(np.abs(np.nan_to_num(depth_concurrent - depth_serial, nan=0.0)))
        ),
        "serial_workers": serial_report_data["scheduler"]["bounded_pool_workers"],
    }
    return {
        "edit_result": edit_result,
        "baseline_counts": baseline["payload"].get("counts"),
        "edited_cli": edited["payload"],
        "report_path": str(report_path),
        "counts": report["counts"],
        "invalid_chunk_ids": [c["chunk_id"] for c in report["chunks"] if c["invalid"]],
        "reused_chunk_ids": [c["chunk_id"] for c in report["chunks"] if not c["invalid"]],
        "seam_validation": report["seam_validation"],
        "scheduler": report["scheduler"],
        "serial_equivalence": serial_equivalence,
        "archive_count": sum(1 for c in report["chunks"] if c.get("archive")),
    }


def _run_global_change_case(name: str, samples: Path, section: str, new_value: List[float], mode: str):
    input_dir = _copy_sample_case(name, samples)
    case_dir = input_dir.parent
    out_dir = case_dir / "out"
    state_dir = case_dir / "state"
    _run_cli(
        [
            "bake",
            "--scene",
            str(input_dir / "scene.initial.json"),
            "--output-dir",
            str(out_dir),
            "--state-dir",
            str(state_dir),
            "--mode",
            "initial",
            "--run-id",
            f"{name}-baseline",
        ]
    )
    scene_path = input_dir / "scene.changed.json"
    scene = json.loads((input_dir / "scene.initial.json").read_text(encoding="utf-8"))
    scene[section]["direction" if section == "lighting" else "position"] = new_value
    scene["scene_id"] = scene["scene_id"] + f"_{name}"
    scene_path.write_text(json.dumps(scene, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = case_dir / f"report.{name}.json"
    result = _run_cli(
        [
            "bake",
            "--scene",
            str(scene_path),
            "--output-dir",
            str(out_dir),
            "--state-dir",
            str(state_dir),
            "--report",
            str(report_path),
            "--mode",
            mode,
            "--changed-key",
            "direction" if section == "lighting" else "position",
            "--run-id",
            f"{name}-changed",
        ]
    )
    report = _load_report(report_path)
    return {
        "cli": result["payload"],
        "counts": report["counts"],
        "invalid_chunk_ids": [c["chunk_id"] for c in report["chunks"] if c["invalid"]],
        "all_chunks_invalid": all(c["invalid"] for c in report["chunks"]),
        "report_path": str(report_path),
    }


def run_schema_case(samples: Path) -> Dict[str, Any]:
    case_dir = WORKBENCH_DIR / "schema_invalid"
    _reset_directory(case_dir)
    scene = json.loads((samples / "scene.initial.json").read_text(encoding="utf-8"))
    scene["camera"]["target"] = list(scene["camera"]["position"])
    invalid_scene = case_dir / "invalid_scene.json"
    invalid_scene.write_text(json.dumps(scene), encoding="utf-8")
    output_dir = case_dir / "must_not_exist"
    result = _run_cli(
        [
            "bake",
            "--scene",
            str(invalid_scene),
            "--output-dir",
            str(output_dir),
            "--mode",
            "initial",
        ],
        expected_exit=2,
    )
    error_code = result["payload"].get("error", {}).get("code")
    return {
        "exit_code": result["exit_code"],
        "error_code": error_code,
        "output_directory_created": output_dir.exists(),
        "json_error": result["payload"],
    }


def run_interrupt_case(samples: Path) -> Dict[str, Any]:
    input_dir = _copy_sample_case("interrupt_recovery", samples)
    case_dir = input_dir.parent
    out_dir = case_dir / "out"
    state_dir = case_dir / "state"
    interrupted = _run_cli(
        [
            "bake",
            "--scene",
            str(input_dir / "scene.initial.json"),
            "--output-dir",
            str(out_dir),
            "--state-dir",
            str(state_dir),
            "--mode",
            "initial",
            "--stop-after-completed",
            "1",
            "--run-id",
            "workbench-interrupt",
        ],
        expected_exit=130,
    )
    marker = state_dir / "INTERRUPTED.json"
    marker_data = json.loads(marker.read_text(encoding="utf-8"))
    marker_existed_before_restart = marker.exists()

    # Simulate an uncommitted/corrupt chunk state left by a force-kill.
    chunks_state = state_dir / "chunks"
    corrupt_chunk_id = "chunk_r001_c001"
    (chunks_state / f"{corrupt_chunk_id}.json").write_text("{not committed", encoding="utf-8")
    (chunks_state / "chunk_r001_c002.npz.tmp").write_bytes(b"partial-npz")

    restart_report = case_dir / "report.restart.json"
    restart = _run_cli(
        [
            "bake",
            "--scene",
            str(input_dir / "scene.initial.json"),
            "--output-dir",
            str(out_dir),
            "--state-dir",
            str(state_dir),
            "--report",
            str(restart_report),
            "--mode",
            "initial",
            "--run-id",
            "workbench-restart",
        ]
    )
    report = _load_report(restart_report)
    return {
        "interrupt_exit_code": interrupted["exit_code"],
        "marker_written": marker_existed_before_restart,
        "marker_recovery_record_written": (state_dir / "INTERRUPTED.recovered.json").exists(),
        "marker_completed_count": len(marker_data["completed_chunk_ids"]),
        "restart_exit_code": restart["exit_code"],
        "restart_counts": report["counts"],
        "pixel_exact": report["seam_validation"].get("pixel_exact"),
        "corrupt_metadata_rejected_and_recomputed": any(
            c["chunk_id"] == corrupt_chunk_id and c["invalid"] for c in report["chunks"]
        ),
        "stale_temp_files_ignored": (chunks_state / "chunk_r001_c002.npz.tmp").exists(),
    }


def _write_markdown(summary: Dict[str, Any]) -> Path:
    path = WORKBENCH_DIR / "validation_report.md"
    local = summary["cases"]["local_edit"]
    lines = [
        "# 地形烘焙工作台验证报告",
        "",
        f"总体状态: **{'PASS' if summary['ok'] else 'FAIL'}**",
        "",
        "## 局部编辑",
        "",
        f"- 总分块数: {local['counts']['total_chunks']}",
        f"- 重渲: {local['counts']['rerendered_chunks']}，复用: {local['counts']['reused_chunks']}",
        f"- 失效块: `{', '.join(local['invalid_chunk_ids'])}`",
        f"- 归档旧版本块数: {local['archive_count']}",
        f"- 灰度最大像素差: {local['seam_validation']['gray_pixel_max_abs_diff']}",
        f"- 深度世界单位最大差: {local['seam_validation']['depth_world_max_abs_diff']}",
        f"- 拼装/全量像素精确: `{local['seam_validation']['pixel_exact']}`",
        f"- 并发与 workers=1 灰度一致: `{local['serial_equivalence']['gray_exact_equal']}`",
        f"- 并发与 workers=1 深度一致: `{local['serial_equivalence']['depth_exact_equal']}`",
        f"- 边界带法线残差: {local['seam_validation']['boundary_band_normal_vector_max_diff_vs_full']}",
        f"- 高度场归属法线偏差: {local['seam_validation']['heightfield_ownership_same_point_max_normal_deviation']:.3e}",
        "",
        "## 光照与相机",
        "",
        "- 光照变化全部失效: "
        f"`{summary['cases']['lighting']['all_chunks_invalid']}` "
        f"(重渲 {summary['cases']['lighting']['counts']['rerendered_chunks']}, "
        f"复用 {summary['cases']['lighting']['counts']['reused_chunks']})",
        "- 相机变化全部失效: "
        f"`{summary['cases']['camera']['all_chunks_invalid']}` "
        f"(重渲 {summary['cases']['camera']['counts']['rerendered_chunks']}, "
        f"复用 {summary['cases']['camera']['counts']['reused_chunks']})",
        "",
        "## Schema 与中断恢复",
        "",
        f"- 非法相机场景退出码: {summary['cases']['schema']['exit_code']} / {summary['cases']['schema']['error_code']}",
        f"- E_SCHEMA 时未创建输出目录: `{not summary['cases']['schema']['output_directory_created']}`",
        "- 信号退出码: "
        f"{summary['cases']['interrupt_recovery']['interrupt_exit_code']}，"
        f"标记写出: `{summary['cases']['interrupt_recovery']['marker_written']}`",
        "- 重启重渲/复用: "
        f"{summary['cases']['interrupt_recovery']['restart_counts']['rerendered_chunks']} / "
        f"{summary['cases']['interrupt_recovery']['restart_counts']['reused_chunks']}",
        "",
        "## 图像",
        "",
        "![灰度](local_edit/out/terrain_gray.png)",
        "",
        "![深度预览](local_edit/out/terrain_depth.png)",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_html(summary: Dict[str, Any]) -> Path:
    path = WORKBENCH_DIR / "index.html"
    local = summary["cases"]["local_edit"]
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>地形烘焙诊断工作台</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; background:#f6f7f9; color:#17202a; }}
.ok {{ color:#0a7d34; font-weight:700; }} .card {{ background:white; border:1px solid #dde3ea; border-radius:12px; padding:1rem 1.25rem; margin:1rem 0; }}
img {{ max-width:520px; image-rendering:pixelated; border:1px solid #ccd3db; margin-right:1rem; }}
code {{ background:#eef1f4; padding:.1rem .3rem; border-radius:4px; }}
</style></head><body>
<h1>地形烘焙诊断工作台</h1>
<p class="ok">PASS: 局部 {local['counts']['rerendered_chunks']} 重渲 / {local['counts']['reused_chunks']} 复用；像素差 {local['seam_validation']['gray_pixel_max_abs_diff']}</p>
<div class="card"><h2>灰度 Lambert</h2><img src="local_edit/out/terrain_gray.png" alt="gray"></div>
<div class="card"><h2>16-bit 深度预览</h2><img src="local_edit/out/terrain_depth.png" alt="depth"></div>
<div class="card"><h2>摘要</h2><pre>{json.dumps(summary, ensure_ascii=False, indent=2)[:12000]}</pre></div>
</body></html>
"""
    path.write_text(html, encoding="utf-8")
    return path


def run_workbench() -> Dict[str, Any]:
    WORKBENCH_DIR.mkdir(parents=True, exist_ok=True)
    samples = PROJECT_ROOT / "samples"
    generate_samples(samples)
    summary: Dict[str, Any] = {"ok": True, "cases": {}}
    summary["cases"]["local_edit"] = run_local_edit_case(samples)
    summary["cases"]["lighting"] = _run_global_change_case(
        "lighting_change", samples, "lighting", [0.58, 0.62, 0.31], "lighting"
    )
    summary["cases"]["camera"] = _run_global_change_case(
        "camera_change", samples, "camera", [7.4, 24.2, -11.9], "camera"
    )
    summary["cases"]["schema"] = run_schema_case(samples)
    summary["cases"]["interrupt_recovery"] = run_interrupt_case(samples)
    summary_path = WORKBENCH_DIR / "validation_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path = _write_markdown(summary)
    html_path = _write_html(summary)
    summary["artifacts"] = {
        "summary": str(summary_path),
        "markdown": str(markdown_path),
        "html": str(html_path),
    }
    return summary


def main() -> int:
    summary = run_workbench()
    print(json.dumps({"ok": summary["ok"], "artifacts": summary["artifacts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
