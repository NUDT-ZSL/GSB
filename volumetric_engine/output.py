"""Final image/depth/report serialization and version-aware atomic commit."""

from __future__ import annotations

import gzip
import io
import json
import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from .errors import EngineError, error
from .imageio import encode_grayscale_png, read_png_dimensions, read_png_dimensions_from_path
from .model import Scene
from .renderer import ImageResult, TRACE_COLUMNS
from .util import atomic_write_bytes, canonical_json_bytes, read_json, sha256_bytes


OUTPUT_FORMAT = "gsb-output-v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def encode_depth_npy(depth: list[list[float]]) -> bytes:
    array = np.asarray(depth, dtype="<f4")
    buffer = io.BytesIO()
    np.save(buffer, array, allow_pickle=False)
    return buffer.getvalue()


def encode_trace_gzip(rows: Sequence[Sequence[Any]]) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=9, mtime=0) as handle:
        header = canonical_json_bytes({"columns": list(TRACE_COLUMNS)}) + b"\n"
        handle.write(header)
        for row in rows:
            handle.write(canonical_json_bytes(list(row)) + b"\n")
    return buffer.getvalue()


def _first_exceed_by_medium(
    trace_rows: Iterable[Sequence[Any]],
    tolerance: float,
) -> dict[str, dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for row in trace_rows:
        medium_id = str(row[4])
        entry = totals.setdefault(
            medium_id,
            {
                "medium_index": int(row[5]),
                "samples": 0,
                "samples_over_tolerance": 0,
                "max_abs_difference": 0.0,
            },
        )
        entry["samples"] += 1
        difference = float(row[13])
        entry["max_abs_difference"] = max(entry["max_abs_difference"], difference)
        if int(row[15]) and difference > tolerance:
            entry["samples_over_tolerance"] += 1
    return dict(sorted(totals.items()))


def build_error_report(
    scene: Scene,
    image: ImageResult,
    version_id: str,
    output_version_id: str,
    *,
    stability: dict[str, Any] | None,
    recovery: dict[str, Any],
    output_names: dict[str, str],
    trace_digest: str,
    trace_bytes: int,
) -> dict[str, Any]:
    tolerance = scene.render.transmittance_tolerance
    first_exceed = sorted(
        (
            pixel.first_exceed
            for pixel in image.pixels
            if pixel.first_exceed is not None
        ),
        key=lambda row: (row["pixel_y"], row["pixel_x"], row["sample"], row["step"]),
    )
    by_medium = _first_exceed_by_medium(image.trace_rows, tolerance)
    reference_changed = sum(
        1
        for y in range(scene.render.height)
        for x in range(scene.render.width)
        if image.gray[y][x] != image.reference_gray[y][x]
    )
    report = {
        "ok": True,
        "format": "gsb-error-report-v1",
        "version_id": output_version_id,
        "render_version_id": version_id,
        "created_utc": _utc_now(),
        "scene": {
            "name": scene.name,
            "seed": scene.seed,
            "media_count": len(scene.media),
            "light_count": len(scene.lights),
            "bounds": {
                "min": list(scene.bounds_min),
                "max": list(scene.bounds_max),
            },
        },
        "render": {
            "width": scene.render.width,
            "height": scene.render.height,
            "samples_per_pixel": scene.render.samples_per_pixel,
            "step_size": scene.render.step_size,
            "max_steps": scene.render.max_steps,
            "transmittance_cutoff": scene.render.transmittance_cutoff,
            "fixed_scale": scene.render.fixed_scale,
            "transmittance_tolerance": tolerance,
            "fixed_arithmetic": (
                "T_fixed=(T_fixed*round(exp(-tau)*scale)+scale//2)//scale; "
                "the final image and verdict are generated only from this path"
            ),
            "float64_role": "diagnostic reference only; never written as final color",
        },
        "summary": {
            **image.stats,
            "reference_changed_pixels": reference_changed,
            "first_exceed_entries": len(first_exceed),
            "trace_rows": len(image.trace_rows),
            "trace_columns": list(TRACE_COLUMNS),
            "trace_file": output_names["trace"],
            "trace_sha256": trace_digest,
            "trace_bytes": trace_bytes,
            "depth_units": "world_units",
            "depth_background": -1.0,
        },
        "first_exceed": first_exceed,
        "by_medium": by_medium,
        "recovery": recovery,
        "outputs": output_names,
        "stability": stability,
        "notes": [
            "逐采样点 float64/定点透射率与绝对差值记录在 trace_file 的压缩 JSONL 中，首行为列名。",
            "fixed_verdict 仅根据定点路径自身的整数乘法舍入偏差相对容差裁定；float64 差值单独记为 diagnostic_exceeded。",
            "重叠介质归属优先级为折射率较高者优先，折射率相同时声明顺序较早者优先；区间归属由区间中点判定。",
        ],
    }
    if stability is None:
        report["stability"] = {"enabled": False}
    return report


def _sha_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise error("E_IO", f"cannot read {path}: {exc}") from exc


def inspect_existing_outputs(output_dir: Path) -> dict[str, Any]:
    manifest_path = output_dir / "manifest.json"
    result: dict[str, Any] = {"manifest_exists": manifest_path.exists(), "version_id": None}
    if not manifest_path.exists():
        return result
    try:
        manifest = read_json(manifest_path, "E_OUTPUT")
    except EngineError as exc:
        result["error"] = exc.message
        return result
    if not isinstance(manifest, dict):
        result["error"] = "output manifest root is not an object"
        return result
    result["version_id"] = manifest.get("version_id")
    result["manifest"] = manifest
    gray_path = output_dir / "grayscale.png"
    result["grayscale_dimensions"] = read_png_dimensions_from_path(gray_path)
    return result


def _existing_bundle_is_valid(
    output_dir: Path,
    version_id: str,
    expected: dict[str, tuple[bytes, int, int | None]],
) -> tuple[bool, str]:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        return False, "manifest missing"
    try:
        manifest = read_json(manifest_path, "E_OUTPUT")
    except EngineError as exc:
        return False, exc.message
    if not isinstance(manifest, dict) or manifest.get("version_id") != version_id:
        return False, "output version differs"
    files = manifest.get("files")
    if not isinstance(files, dict):
        return False, "manifest files missing"
    for name, (_, _, expected_width) in expected.items():
        info = files.get(name)
        if not isinstance(info, dict):
            return False, f"manifest entry missing for {name}"
        path = output_dir / name
        if not path.exists():
            return False, f"output file missing: {name}"
        try:
            data = path.read_bytes()
        except OSError as exc:
            return False, f"cannot read output {name}: {exc}"
        if len(data) != info.get("bytes") or sha256_bytes(data) != info.get("sha256"):
            return False, f"digest mismatch for {name}"
        if expected_width is not None:
            try:
                width, height = read_png_dimensions(data)
            except EngineError as exc:
                return False, f"{name}: {exc.message}"
            if width != expected_width or height != manifest.get("height"):
                return False, f"{name} dimensions differ from manifest"
    return True, "valid"


def _dtype_info(data: bytes, name: str) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return read_png_dimensions(data)
    return None


def _write_temp(target: Path, data: bytes) -> Path:
    temp_path = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise error("E_IO", f"cannot create temporary output {temp_path}: {exc}") from exc
    return temp_path


def write_outputs(
    scene: Scene,
    image: ImageResult,
    version_id: str,
    report: dict[str, Any],
    trace_rows: list[tuple[Any, ...]],
    *,
    trace_bytes: bytes | None = None,
) -> dict[str, Any]:
    output_dir = scene.render.output_dir
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise error("E_OUTPUT", f"cannot create output directory: {exc}", str(output_dir)) from exc

    gray_bytes = encode_grayscale_png(image.gray, scene.render.width, scene.render.height)
    depth_bytes = encode_depth_npy(image.depth)
    if trace_bytes is None:
        trace_bytes = encode_trace_gzip(trace_rows)
    trace_digest = sha256_bytes(trace_bytes)
    report = dict(report)
    report["summary"] = dict(report["summary"])
    report["summary"]["trace_sha256"] = trace_digest
    report["summary"]["trace_bytes"] = len(trace_bytes)
    report_bytes = canonical_json_bytes(report) + b"\n"
    files: dict[str, bytes] = {
        "grayscale.png": gray_bytes,
        "depth.npy": depth_bytes,
        "error_trace.jsonl.gz": trace_bytes,
        "error_report.json": report_bytes,
    }
    existing = inspect_existing_outputs(output_dir)
    expected = {name: (data, len(data), scene.render.width if name == "grayscale.png" else None) for name, data in files.items()}
    expected["error_report.json"] = (report_bytes, len(report_bytes), None)
    expected["depth.npy"] = (depth_bytes, len(depth_bytes), None)
    expected["error_trace.jsonl.gz"] = (trace_bytes, len(trace_bytes), None)
    valid, reason = _existing_bundle_is_valid(output_dir, version_id, expected)
    if valid:
        return {
            "written": False,
            "reason": "same version and valid output bundle already exists",
            "version_id": version_id,
            "existing": existing,
            "output_dir": str(output_dir),
        }

    manifest_files: dict[str, Any] = {}
    temp_paths: list[tuple[Path, Path]] = []
    try:
        for name, data in files.items():
            target = output_dir / name
            temp_path = _write_temp(target, data)
            temp_paths.append((temp_path, target))
            info: dict[str, Any] = {
                "sha256": sha256_bytes(data),
                "bytes": len(data),
            }
            dimensions = _dtype_info(data, name)
            if dimensions is not None:
                info["width"], info["height"] = dimensions
            manifest_files[name] = info
        manifest = {
            "format": OUTPUT_FORMAT,
            "version_id": version_id,
            "width": scene.render.width,
            "height": scene.render.height,
            "created_utc": _utc_now(),
            "files": manifest_files,
        }
        manifest_bytes = canonical_json_bytes(manifest) + b"\n"
        manifest_temp = _write_temp(output_dir / "manifest.json", manifest_bytes)
        temp_paths.append((manifest_temp, output_dir / "manifest.json"))
        for temp_path, target in temp_paths:
            os.replace(temp_path, target)
    except OSError as exc:
        for temp_path, _ in temp_paths:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise error("E_OUTPUT", f"cannot commit output bundle: {exc}", str(output_dir)) from exc
    return {
        "written": True,
        "reason": "output version changed or previous bundle was invalid",
        "previous": existing,
        "version_id": version_id,
        "output_dir": str(output_dir),
        "files": manifest_files,
    }

