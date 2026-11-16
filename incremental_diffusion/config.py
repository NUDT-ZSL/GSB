"""Configuration loading, validation, and normalized content semantics."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, UnidentifiedImageError

from .canonical import array_fingerprint, hash_object
from .errors import E_CONFIG_INVALID, E_NOISE_MISMATCH, E_PATCH_OUT_OF_RANGE, WorkbenchError


SCHEMA_VERSION = 1
REQUIRED_FIELDS = (
    "schema_version",
    "snapshot_version",
    "seed",
    "prompt",
    "negative_prompt",
    "steps",
    "guidance_scale",
    "width",
    "height",
    "latent_channels",
    "embedding_dim",
    "vocab",
    "noise_table",
    "output_image",
    "report_json",
    "graph_png",
)


def _invalid(message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError(E_CONFIG_INVALID, message, details=details)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require(raw: dict[str, Any], field: str) -> Any:
    if field not in raw:
        raise _invalid(f"missing required field: {field}", field=field)
    return raw[field]


def _int_field(raw: dict[str, Any], field: str, minimum: int, maximum: int) -> int:
    value = _require(raw, field)
    if not _is_int(value):
        raise _invalid(f"field {field} must be an integer", field=field)
    if value < minimum or value > maximum:
        raise _invalid(
            f"field {field} must be in [{minimum}, {maximum}]", field=field, value=value
        )
    return value


def _float_field(
    raw: dict[str, Any], field: str, minimum: float, maximum: float, *, default: float | None = None
) -> float:
    value = raw.get(field, default) if default is not None else _require(raw, field)
    if not _is_number(value):
        raise _invalid(f"field {field} must be numeric", field=field)
    number = float(value)
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise _invalid(
            f"field {field} must be finite and in [{minimum}, {maximum}]",
            field=field,
            value=value,
        )
    return number


def _string_field(raw: dict[str, Any], field: str) -> str:
    value = _require(raw, field)
    if not isinstance(value, str):
        raise _invalid(f"field {field} must be a string", field=field)
    return value


def _resolve_path(base_dir: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise _invalid(f"field {field} must be a non-empty path string", field=field)
    path = Path(value)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


@dataclass(frozen=True)
class Patch:
    step: int
    x: int
    y: int
    width: int
    height: int
    values: np.ndarray

    @property
    def values_fingerprint(self) -> str:
        return array_fingerprint(self.values)

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "values_fingerprint": self.values_fingerprint,
        }


@dataclass
class LoadedConfig:
    raw: dict[str, Any]
    path: Path
    base_dir: Path
    schema_version: int
    snapshot_version: int
    seed: int
    prompt: str
    negative_prompt: str
    steps: int
    guidance_scale: float
    width: int
    height: int
    latent_channels: int
    embedding_dim: int
    vocab: list[str]
    img2img_strength: float
    skip_steps: int
    noise_table: np.ndarray
    noise_table_fingerprint: str
    initial_image: np.ndarray | None
    initial_image_fingerprint: str | None
    patches: list[Patch]
    patches_by_step: dict[int, list[Patch]]
    workspace: Path
    output_image: Path
    report_json: Path
    graph_png: Path
    intermediates_dir: Path
    semantic_config: dict[str, Any]
    config_hash: str

    @property
    def img2img_active(self) -> bool:
        return self.initial_image is not None and self.skip_steps > 0


def read_raw_config(path: Path | str) -> dict[str, Any]:
    config_path = Path(path)
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise _invalid(f"config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise _invalid(
            f"config JSON is invalid: {exc.msg}", line=exc.lineno, column=exc.colno
        ) from exc
    except OSError as exc:
        raise _invalid(f"cannot read config: {type(exc).__name__}: {exc}") from exc
    if not isinstance(raw, dict):
        raise _invalid("config root must be a JSON object")
    return raw


def _load_noise_table(
    path: Path,
    *,
    steps: int,
    latent_channels: int,
    width: int,
    height: int,
) -> np.ndarray:
    try:
        table = np.load(path, allow_pickle=False)
    except FileNotFoundError as exc:
        raise _invalid(f"noise table not found: {path}", field="noise_table") from exc
    except (OSError, ValueError) as exc:
        raise _invalid(
            f"cannot load noise table {path}: {type(exc).__name__}: {exc}",
            field="noise_table",
        ) from exc
    table = np.asarray(table)
    expected = (steps, latent_channels, height, width)
    if table.shape != expected:
        raise WorkbenchError(
            E_NOISE_MISMATCH,
            f"noise table shape {list(table.shape)} does not match expected {list(expected)}",
            details={"actual_shape": list(table.shape), "expected_shape": list(expected)},
        )
    if not np.issubdtype(table.dtype, np.number):
        raise WorkbenchError(
            E_NOISE_MISMATCH,
            f"noise table dtype {table.dtype} is not numeric", 
        )
    table = np.ascontiguousarray(table, dtype="<f8")
    if not np.all(np.isfinite(table)):
        raise WorkbenchError(E_NOISE_MISMATCH, "noise table contains non-finite values")
    return table


def _load_initial_image(path: Path, *, width: int, height: int) -> np.ndarray:
    try:
        with Image.open(path) as image:
            rgb = np.asarray(image.convert("RGB"), dtype=np.uint8).copy()
    except FileNotFoundError as exc:
        raise _invalid(f"initial image not found: {path}", field="initial_image") from exc
    except (OSError, UnidentifiedImageError) as exc:
        raise _invalid(
            f"cannot read initial image {path}: {type(exc).__name__}: {exc}",
            field="initial_image",
        ) from exc
    if rgb.shape != (height, width, 3):
        raise _invalid(
            f"initial image shape {list(rgb.shape)} does not match expected "
            f"{[height, width, 3]}",
            field="initial_image",
        )
    return rgb


def _parse_vocab(raw: dict[str, Any], embedding_dim: int) -> list[str]:
    value = _require(raw, "vocab")
    if not isinstance(value, list) or not value:
        raise _invalid("field vocab must be a non-empty list of strings", field="vocab")
    if any(not isinstance(item, str) or not item for item in value):
        raise _invalid("every vocab entry must be a non-empty string", field="vocab")
    normalized = [item.strip().lower() for item in value]
    if any(not item for item in normalized):
        raise _invalid("vocab entries may not become empty after normalization", field="vocab")
    if len(set(normalized)) != len(normalized):
        raise _invalid("vocab entries must be unique after normalization", field="vocab")
    if embedding_dim < 8:
        raise _invalid("embedding_dim must be at least 8", field="embedding_dim")
    return normalized


def _patch_int(patch: dict[str, Any], name: str, step: int) -> int:
    value = _require(patch, name)
    if not _is_int(value):
        raise _invalid(f"patch[{step}].{name} must be an integer", field=f"patches[{step}].{name}")
    return value


def _parse_patches(
    raw: dict[str, Any],
    *,
    steps: int,
    width: int,
    height: int,
    latent_channels: int,
) -> tuple[list[Patch], dict[int, list[Patch]]]:
    value = raw.get("patches", [])
    if not isinstance(value, list):
        raise _invalid("field patches must be a list", field="patches")
    patches: list[Patch] = []
    by_step: dict[int, list[Patch]] = {}
    for index, item in enumerate(value):
        path = f"patches[{index}]"
        if not isinstance(item, dict):
            raise _invalid(f"{path} must be an object", field=path)
        step = _patch_int(item, "step", index)
        x = _patch_int(item, "x", index)
        y = _patch_int(item, "y", index)
        patch_width = _patch_int(item, "width", index)
        patch_height = _patch_int(item, "height", index)
        if step < 0 or step >= steps:
            raise WorkbenchError(
                E_PATCH_OUT_OF_RANGE,
                f"patch step {step} is outside [0, {steps - 1}]",
                details={"patch_index": index, "field": "step", "value": step},
            )
        if (
            x < 0
            or y < 0
            or patch_width <= 0
            or patch_height <= 0
            or x + patch_width > width
            or y + patch_height > height
        ):
            raise WorkbenchError(
                E_PATCH_OUT_OF_RANGE,
                f"patch rectangle (x={x}, y={y}, width={patch_width}, height={patch_height}) "
                f"is outside {width}x{height}",
                details={
                    "patch_index": index,
                    "x": x,
                    "y": y,
                    "width": patch_width,
                    "height": patch_height,
                    "image_width": width,
                    "image_height": height,
                },
            )
        if "values" not in item:
            raise _invalid(f"missing required field: {path}.values", field=f"{path}.values")
        try:
            values = np.asarray(item["values"], dtype="<f8")
        except (TypeError, ValueError) as exc:
            raise _invalid(
                f"{path}.values must be a numeric array", field=f"{path}.values"
            ) from exc
        expected = (patch_height, patch_width, latent_channels)
        if values.size == int(np.prod(expected)) and values.shape != expected:
            values = values.reshape(expected)
        if values.shape != expected:
            raise _invalid(
                f"{path}.values shape {list(values.shape)} does not match expected "
                f"{list(expected)}",
                field=f"{path}.values",
            )
        if not np.all(np.isfinite(values)):
            raise _invalid(f"{path}.values contains non-finite values", field=f"{path}.values")
        patch = Patch(
            step=step,
            x=x,
            y=y,
            width=patch_width,
            height=patch_height,
            values=np.ascontiguousarray(values, dtype="<f8"),
        )
        patches.append(patch)
        by_step.setdefault(step, []).append(patch)
    return patches, by_step


def load_config(path: Path | str) -> LoadedConfig:
    config_path = Path(path).resolve()
    raw = read_raw_config(config_path)
    base_dir = config_path.parent

    missing = [field for field in REQUIRED_FIELDS if field not in raw]
    if missing:
        raise _invalid(
            f"missing required field(s): {', '.join(missing)}", missing_fields=missing
        )

    schema_version = _int_field(raw, "schema_version", SCHEMA_VERSION, SCHEMA_VERSION)
    snapshot_version = _int_field(raw, "snapshot_version", 1, 2**31 - 1)
    seed = _int_field(raw, "seed", 0, 2**32 - 1)
    prompt = _string_field(raw, "prompt")
    negative_prompt = _string_field(raw, "negative_prompt")
    steps = _int_field(raw, "steps", 1, 16)
    guidance_scale = _float_field(raw, "guidance_scale", 0.0, 30.0)
    width = _int_field(raw, "width", 8, 128)
    height = _int_field(raw, "height", 8, 128)
    latent_channels = _int_field(raw, "latent_channels", 1, 16)
    embedding_dim = _int_field(raw, "embedding_dim", 8, 256)
    vocab = _parse_vocab(raw, embedding_dim)
    default_img2img_strength = 0.5 if isinstance(raw.get("initial_image"), str) else 0.0
    img2img_strength = _float_field(
        raw, "img2img_strength", 0.0, 1.0, default=default_img2img_strength
    )

    noise_path = _resolve_path(base_dir, raw["noise_table"], "noise_table")
    noise_table = _load_noise_table(
        noise_path,
        steps=steps,
        latent_channels=latent_channels,
        width=width,
        height=height,
    )

    initial_image: np.ndarray | None = None
    initial_image_fingerprint: str | None = None
    if "initial_image" in raw and raw["initial_image"] is not None:
        image_path = _resolve_path(base_dir, raw["initial_image"], "initial_image")
        initial_image = _load_initial_image(image_path, width=width, height=height)
        initial_image_fingerprint = array_fingerprint(initial_image)

    patches, patches_by_step = _parse_patches(
        raw,
        steps=steps,
        width=width,
        height=height,
        latent_channels=latent_channels,
    )
    skip_steps = (
        min(steps, int(math.floor(steps * img2img_strength + 0.5)))
        if initial_image is not None
        else 0
    )

    workspace = _resolve_path(base_dir, raw.get("workspace", "workspace"), "workspace")
    output_image = _resolve_path(base_dir, raw["output_image"], "output_image")
    report_json = _resolve_path(base_dir, raw["report_json"], "report_json")
    graph_png = _resolve_path(base_dir, raw["graph_png"], "graph_png")
    intermediates_dir = _resolve_path(
        base_dir,
        raw.get("intermediates_dir", str(output_image.parent / "intermediates")),
        "intermediates_dir",
    )

    semantic_config: dict[str, Any] = {
        "schema_version": schema_version,
        "snapshot_version": snapshot_version,
        "seed": seed,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "steps": steps,
        "guidance_scale": guidance_scale,
        "width": width,
        "height": height,
        "latent_channels": latent_channels,
        "embedding_dim": embedding_dim,
        "vocab": vocab,
        "img2img_strength": img2img_strength,
        "skip_steps": skip_steps,
        "noise_table_fingerprint": array_fingerprint(noise_table),
        "initial_image_fingerprint": initial_image_fingerprint,
        "patches": [patch.semantic_dict() for patch in patches],
    }
    config_hash = hash_object(semantic_config)

    return LoadedConfig(
        raw=raw,
        path=config_path,
        base_dir=base_dir,
        schema_version=schema_version,
        snapshot_version=snapshot_version,
        seed=seed,
        prompt=prompt,
        negative_prompt=negative_prompt,
        steps=steps,
        guidance_scale=guidance_scale,
        width=width,
        height=height,
        latent_channels=latent_channels,
        embedding_dim=embedding_dim,
        vocab=vocab,
        img2img_strength=img2img_strength,
        skip_steps=skip_steps,
        noise_table=noise_table,
        noise_table_fingerprint=array_fingerprint(noise_table),
        initial_image=initial_image,
        initial_image_fingerprint=initial_image_fingerprint,
        patches=patches,
        patches_by_step=patches_by_step,
        workspace=workspace,
        output_image=output_image,
        report_json=report_json,
        graph_png=graph_png,
        intermediates_dir=intermediates_dir,
        semantic_config=semantic_config,
        config_hash=config_hash,
    )


def infer_diagnostic_paths(
    config_path: Path | str,
    *,
    report_override: Path | str | None = None,
    graph_override: Path | str | None = None,
) -> tuple[Path, Path | None]:
    """Best-effort report/graph paths for errors that occur before full validation."""
    path = Path(config_path).resolve()
    base_dir = path.parent
    raw: dict[str, Any] = {}
    try:
        candidate = read_raw_config(path)
        if isinstance(candidate, dict):
            raw = candidate
    except WorkbenchError:
        pass
    report_path = Path(report_override).resolve() if report_override else None
    if report_path is None:
        value = raw.get("report_json")
        report_path = (
            _resolve_path(base_dir, value, "report_json")
            if isinstance(value, str) and value
            else (Path.cwd() / "diagnostic.json").resolve()
        )
    graph_path: Path | None = None
    if graph_override:
        graph_path = Path(graph_override).resolve()
    else:
        value = raw.get("graph_png")
        if isinstance(value, str) and value:
            graph_path = _resolve_path(base_dir, value, "graph_png")
    return report_path, graph_path
