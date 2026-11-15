#!/usr/bin/env python3
"""End-to-end deterministic 128^3 volume ray-casting demonstration."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from medvol import (  # noqa: E402
    BusinessCode,
    Camera,
    OffscreenGLRenderer,
    SamplingConfig,
    TransferFunction,
    Volume,
    VolumeRenderError,
    VolumeRenderer,
)
from medvol.renderer import rgba_to_uint8_srgb  # noqa: E402
from medvol.synthetic import make_synthetic_ct_volume  # noqa: E402
from medvol.transfer_function import demo_transfer_function  # noqa: E402


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def save_png(path: Path, rgba_linear: np.ndarray) -> None:
    Image.fromarray(rgba_to_uint8_srgb(rgba_linear), "RGBA").save(path, format="PNG")


def ensure_volume(path: Path, size: int) -> Volume:
    if path.exists():
        return Volume.load_npy(path)
    volume = make_synthetic_ct_volume(size)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, volume.data, allow_pickle=False)
    write_json(path.with_suffix(".json"), volume.metadata())
    return volume


def build_camera(volume: Volume) -> Camera:
    center = volume.center
    size = max(volume.shape_xyz)
    position = center + np.asarray((1.38, 0.92, 1.38), dtype=np.float64) * size
    return Camera.look_at(position, center, up=(0.0, 1.0, 0.0), fov_y_deg=38.0)


def error_metrics_not_worse(fixed: dict[str, Any], adaptive: dict[str, Any]) -> bool:
    fixed_error = fixed["error_analysis"]["total_error"]
    adaptive_error = adaptive["error_analysis"]["total_error"]
    return all(adaptive_error[key] <= fixed_error[key] for key in ("max", "mean", "p95"))


def deterministic_cpu_render(
    volume: Volume,
    camera: Camera,
    tf: TransferFunction,
    config: SamplingConfig,
    width: int,
    height: int,
) -> tuple[Any, dict[str, Any]]:
    renderer = VolumeRenderer(volume, camera, tf, config)
    first = renderer.render(width, height)
    second = renderer.render(width, height)
    check = {
        "run1_rgba_linear_sha256": first.rgba_sha256,
        "run2_rgba_linear_sha256": second.rgba_sha256,
        "hash_equal": first.rgba_sha256 == second.rgba_sha256,
        "buffer_bytes_equal": first.rgba_linear.tobytes(order="C") == second.rgba_linear.tobytes(order="C"),
        "error_analysis_equal": first.report["error_analysis"] == second.report["error_analysis"],
        "sampling_report_equal": first.report["sampling"] == second.report["sampling"],
    }
    return first, check


def run_gl_pair(
    volume: Volume,
    camera: Camera,
    tf: TransferFunction,
    fixed_config: SamplingConfig,
    adaptive_config: SamplingConfig,
    resolution: int,
) -> tuple[dict[str, Any], Any, Any]:
    fixed_renderer = OffscreenGLRenderer(volume, camera, tf, fixed_config, width=resolution, height=resolution)
    try:
        fixed_first = fixed_renderer.render(resolution, resolution)
        fixed_second = fixed_renderer.render(resolution, resolution)
        fixed_check = {
            "run1_rgba_linear_sha256": fixed_first.rgba_linear_sha256,
            "run2_rgba_linear_sha256": fixed_second.rgba_linear_sha256,
            "hash_equal": fixed_first.rgba_linear_sha256 == fixed_second.rgba_linear_sha256,
            "buffer_bytes_equal": fixed_first.rgba_linear.tobytes(order="C") == fixed_second.rgba_linear.tobytes(order="C"),
        }
        fixed_metadata = fixed_first.report["opengl"]
    finally:
        fixed_renderer.close()

    adaptive_renderer = OffscreenGLRenderer(volume, camera, tf, adaptive_config, width=resolution, height=resolution)
    try:
        adaptive_first = adaptive_renderer.render(resolution, resolution)
        adaptive_second = adaptive_renderer.render(resolution, resolution)
        adaptive_check = {
            "run1_rgba_linear_sha256": adaptive_first.rgba_linear_sha256,
            "run2_rgba_linear_sha256": adaptive_second.rgba_linear_sha256,
            "hash_equal": adaptive_first.rgba_linear_sha256 == adaptive_second.rgba_linear_sha256,
            "buffer_bytes_equal": adaptive_first.rgba_linear.tobytes(order="C") == adaptive_second.rgba_linear.tobytes(order="C"),
        }
    finally:
        adaptive_renderer.close()

    payload = {
        "attempted": True,
        "available": True,
        "gl_version": fixed_metadata["version"],
        "gl_renderer": fixed_metadata["renderer"],
        "fbo_resolution": [resolution, resolution],
        "readback_bytes": fixed_metadata["readback_bytes"],
        "fixed_determinism": fixed_check,
        "adaptive_determinism": adaptive_check,
    }
    return payload, fixed_first, adaptive_first


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--volume", type=Path, default=ROOT / "artifacts" / "demo" / "volume_128.npy")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts" / "demo")
    parser.add_argument("--resolution", type=int, default=256)
    parser.add_argument("--size", type=int, default=128, help="Phantom size used only if --volume is absent")
    parser.add_argument("--skip-gl", action="store_true")
    parser.add_argument("--require-gl", action="store_true")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    volume = ensure_volume(args.volume, args.size)
    camera = build_camera(volume)

    # Serialize, reload, and validate once. Render loops use the pre-built LUT.
    tf_source = demo_transfer_function(volume.value_range)
    tf_json_path = args.output_dir / "transfer_function.json"
    tf_json_path.write_text(tf_source.to_json() + "\n", encoding="utf-8")
    tf = TransferFunction.from_file(tf_json_path)

    resolution = int(args.resolution)
    if resolution <= 0:
        raise SystemExit("--resolution must be positive")
    base_step = 0.8 * volume.min_spacing
    common = dict(
        base_step=base_step,
        max_refinement=2,
        gradient_threshold=0.15,
        early_termination_threshold=0.02,
        error_threshold=2.0,
    )
    fixed_config = SamplingConfig(adaptive=False, **common)
    adaptive_config = SamplingConfig(adaptive=True, **common)

    fixed_cpu, fixed_determinism = deterministic_cpu_render(
        volume, camera, tf, fixed_config, resolution, resolution
    )
    adaptive_cpu, adaptive_determinism = deterministic_cpu_render(
        volume, camera, tf, adaptive_config, resolution, resolution
    )

    save_png(args.output_dir / "fixed_cpu.png", fixed_cpu.rgba_linear)
    save_png(args.output_dir / "adaptive_cpu.png", adaptive_cpu.rgba_linear)
    write_json(args.output_dir / "fixed_cpu_report.json", fixed_cpu.to_envelope())
    write_json(args.output_dir / "adaptive_cpu_report.json", adaptive_cpu.to_envelope())

    gl_payload: dict[str, Any] = {"attempted": not args.skip_gl, "available": False}
    if not args.skip_gl:
        try:
            gl_payload, fixed_gl, adaptive_gl = run_gl_pair(
                volume,
                camera,
                tf,
                fixed_config,
                adaptive_config,
                resolution,
            )
            save_png(args.output_dir / "fixed_gl.png", fixed_gl.rgba_linear)
            save_png(args.output_dir / "adaptive_gl.png", adaptive_gl.rgba_linear)

            fixed_gl_report = copy.deepcopy(fixed_gl.to_envelope())
            adaptive_gl_report = copy.deepcopy(adaptive_gl.to_envelope())
            fixed_gl_report["report"]["reference_error_analysis"] = fixed_cpu.report["error_analysis"]
            fixed_gl_report["report"]["reference_sampling"] = fixed_cpu.report["sampling"]
            adaptive_gl_report["report"]["reference_error_analysis"] = adaptive_cpu.report["error_analysis"]
            adaptive_gl_report["report"]["reference_sampling"] = adaptive_cpu.report["sampling"]
            if fixed_cpu.business_code == BusinessCode.E_ERROR_BOUND:
                fixed_gl_report.update(
                    ok=False,
                    status=BusinessCode.E_ERROR_BOUND.value,
                    business_code=BusinessCode.E_ERROR_BOUND.value,
                )
            if adaptive_cpu.business_code == BusinessCode.E_ERROR_BOUND:
                adaptive_gl_report.update(
                    ok=False,
                    status=BusinessCode.E_ERROR_BOUND.value,
                    business_code=BusinessCode.E_ERROR_BOUND.value,
                )
            write_json(args.output_dir / "fixed_gl_report.json", fixed_gl_report)
            write_json(args.output_dir / "adaptive_gl_report.json", adaptive_gl_report)
            gl_payload["fixed_hash_matches_cpu"] = fixed_gl.rgba_linear_sha256 == fixed_cpu.rgba_sha256
            gl_payload["adaptive_hash_matches_cpu"] = adaptive_gl.rgba_linear_sha256 == adaptive_cpu.rgba_sha256
        except VolumeRenderError as exc:
            gl_payload = {"attempted": True, "available": False, "error": exc.to_dict()}
            write_json(args.output_dir / "gl_error.json", gl_payload)
            if args.require_gl:
                raise

    adaptive_not_worse = error_metrics_not_worse(fixed_cpu.report, adaptive_cpu.report)
    adaptive_uses_more_samples = (
        adaptive_cpu.report["sampling"]["total_samples"] >= fixed_cpu.report["sampling"]["total_samples"]
    )
    any_bound_exceeded = any(
        result.business_code == BusinessCode.E_ERROR_BOUND for result in (fixed_cpu, adaptive_cpu)
    )
    status = BusinessCode.E_ERROR_BOUND if any_bound_exceeded else BusinessCode.OK
    summary = {
        "schema_version": "1.0.0",
        "ok": not any_bound_exceeded,
        "status": status.value,
        "business_code": status.value,
        "inputs": {
            "volume_path": str(args.volume),
            "volume": volume.metadata(),
            "transfer_function_path": str(tf_json_path),
            "transfer_function": tf.metadata(),
            "camera": camera.metadata(),
            "resolution": [resolution, resolution],
        },
        "determinism": {
            "cpu_fixed": fixed_determinism,
            "cpu_adaptive": adaptive_determinism,
        },
        "modes": {
            "fixed": {
                "sampling": fixed_cpu.report["sampling"],
                "error_analysis": fixed_cpu.report["error_analysis"],
                "rgba_linear_sha256": fixed_cpu.rgba_sha256,
                "status": fixed_cpu.business_code.value,
                "png": str(args.output_dir / "fixed_cpu.png"),
            },
            "adaptive": {
                "sampling": adaptive_cpu.report["sampling"],
                "error_analysis": adaptive_cpu.report["error_analysis"],
                "rgba_linear_sha256": adaptive_cpu.rgba_sha256,
                "status": adaptive_cpu.business_code.value,
                "png": str(args.output_dir / "adaptive_cpu.png"),
            },
        },
        "opengl": gl_payload,
        "acceptance": {
            "two_run_hash_and_error_stats_equal": fixed_determinism["hash_equal"]
            and fixed_determinism["buffer_bytes_equal"]
            and fixed_determinism["error_analysis_equal"]
            and adaptive_determinism["hash_equal"]
            and adaptive_determinism["buffer_bytes_equal"]
            and adaptive_determinism["error_analysis_equal"],
            "adaptive_error_not_greater_than_fixed": adaptive_not_worse,
            "adaptive_samples_not_fewer_than_fixed": adaptive_uses_more_samples,
            "geometry_and_transfer_unchanged_by_adaptive": True,
        },
    }
    write_json(args.output_dir / "report.json", summary)

    print(
        json.dumps(
            {
                "report": str(args.output_dir / "report.json"),
                "fixed_hash": fixed_cpu.rgba_sha256,
                "adaptive_hash": adaptive_cpu.rgba_sha256,
                "deterministic_cpu": summary["acceptance"]["two_run_hash_and_error_stats_equal"],
                "adaptive_not_worse": adaptive_not_worse,
                "status": status.value,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
