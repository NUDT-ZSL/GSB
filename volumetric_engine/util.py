"""Small deterministic and atomic-I/O helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable

from .errors import EngineError, error


ALGORITHM_VERSION = "fixed-point-volumetric-v1"


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def finite_number(value: Any) -> bool:
    return is_number(value) and float(value) == float(value) and abs(float(value)) != float("inf")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes through a same-directory temp file and atomic replace."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_dir(path.parent)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise error("E_IO", f"atomic write failed for {path}: {exc}") from exc


def atomic_write_json(path: Path, value: Any) -> str:
    data = canonical_json_bytes(value)
    atomic_write_bytes(path, data)
    return sha256_bytes(data)


def read_json(path: Path, code: str = "E_JSON") -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise error("E_IO", f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise error(code, f"invalid JSON in {path}: {exc}", str(path)) from exc


def probe_directory(path: Path, code: str) -> None:
    """Create and atomically replace a probe file, then remove it."""

    path = Path(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise error(code, f"cannot create directory {path}: {exc}", str(path)) from exc
    first = path / f".probe-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    second = path / f".probe-{os.getpid()}-{uuid.uuid4().hex}.replaced"
    try:
        with open(first, "wb") as handle:
            handle.write(b"probe")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(first, second)
        second.unlink()
        _fsync_dir(path)
    except OSError as exc:
        raise error(code, f"directory is not usable for atomic writes: {path}: {exc}", str(path)) from exc
    finally:
        for candidate in (first, second):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass


def normalized_float(value: float, digits: int = 12) -> float:
    return float(f"{float(value):.{digits}g}")


def canonical_media_light_camera(scene: Any) -> dict[str, Any]:
    """Return the canonical semantic payload used by render/version hashes."""

    media = []
    for item in scene.media:
        media.append(
            {
                "id": item.id,
                "kind": item.kind,
                "center": [normalized_float(v) for v in item.center],
                "size": [normalized_float(v) for v in item.size],
                "extinction": [normalized_float(v) for v in item.extinction],
                "albedo": [normalized_float(v) for v in item.albedo],
                "ior": normalized_float(item.ior),
                "declaration_index": item.declaration_index,
            }
        )
    lights = [
        {
            "id": light.id,
            "position": [normalized_float(v) for v in light.position],
            "intensity": normalized_float(light.intensity),
        }
        for light in scene.lights
    ]
    camera = {
        "position": [normalized_float(v) for v in scene.camera.position],
        "forward": [normalized_float(v) for v in scene.camera.forward],
        "up": [normalized_float(v) for v in scene.camera.up],
        "vertical_fov_deg": normalized_float(scene.camera.vertical_fov_deg),
        "aspect_ratio": normalized_float(scene.camera.aspect_ratio),
    }
    render = {
        "width": scene.render.width,
        "height": scene.render.height,
        "samples_per_pixel": scene.render.samples_per_pixel,
        "step_size": normalized_float(scene.render.step_size),
        "max_steps": scene.render.max_steps,
        "transmittance_cutoff": normalized_float(scene.render.transmittance_cutoff),
        "fixed_scale": scene.render.fixed_scale,
        "transmittance_tolerance": normalized_float(scene.render.transmittance_tolerance),
        "ray_epsilon": normalized_float(scene.render.ray_epsilon),
        "exposure": normalized_float(scene.render.exposure),
        "tile": {
            "width": scene.render.tile_width,
            "height": scene.render.tile_height,
        },
        "sample_offsets": [
            [normalized_float(x), normalized_float(y)] for x, y in scene.jitter.sample_offsets
        ],
    }
    return {
        "algorithm": ALGORITHM_VERSION,
        "media": media,
        "lights": lights,
        "camera": camera,
        "render": render,
    }


def version_id_for(scene: Any, prefix: str = "v1") -> str:
    return f"{prefix}-{sha256_json(canonical_media_light_camera(scene))[:32]}"


def mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0

