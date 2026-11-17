from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from PIL import Image, UnidentifiedImageError

from .errors import ConfigError, InitImageError, NoiseMismatchError, PatchRangeError
from .util import array_fingerprint, json_safe, sha256_bytes, sha256_json


SUPPORTED_MODES = {"t2v", "i2v"}
SUPPORTED_INJECTION_STAGES = {
    "encode",
    "init",
    "denoise",
    "decode",
    "preview",
}


@dataclass
class PatchSpec:
    step: int
    frame: int | None
    x: int
    y: int
    values: np.ndarray
    fingerprint: str
    rect: dict[str, int]

    @property
    def height(self) -> int:
        return int(self.values.shape[0])

    @property
    def width(self) -> int:
        return int(self.values.shape[1])

    def applies_to(self, step: int, frame: int) -> bool:
        return self.step == step and (self.frame is None or self.frame == frame)


@dataclass
class InitImage:
    path: Path
    pixels: np.ndarray
    fingerprint: str


@dataclass
class ValidatedConfig:
    raw: dict[str, Any]
    base_dir: Path
    config_version: int
    mode: str
    prompt: str
    seed: int
    steps: int
    guidance: float
    frames: int
    latent_shape: tuple[int, int, int]
    output_size: tuple[int, int]
    embed_dim: int
    max_workers: int
    preview_size: tuple[int, int]
    preview_budget_ms: int
    stage_delay_ms: int
    output_dir: Path
    vocab_path: Path
    vocab: tuple[str, ...]
    vocab_fingerprint: str
    noise_table_path: Path
    noise_table: np.ndarray
    noise_fingerprint: str
    init_image: InitImage | None
    i2v_replacement_steps: int
    patch: PatchSpec | None
    inject_failures: tuple[dict[str, Any], ...]
    config_fingerprint: str
    tokens: tuple[str, ...] = field(default_factory=tuple)

    @property
    def latent_h(self) -> int:
        return self.latent_shape[0]

    @property
    def latent_w(self) -> int:
        return self.latent_shape[1]

    @property
    def latent_c(self) -> int:
        return self.latent_shape[2]

    @property
    def width(self) -> int:
        return self.output_size[0]

    @property
    def height(self) -> int:
        return self.output_size[1]

    def injection_for(self, stage: str, step: int | None, frame: int | None) -> dict[str, Any] | None:
        for item in self.inject_failures:
            if item["stage"] != stage:
                continue
            if item.get("step") is not None and item["step"] != step:
                continue
            if item.get("frame") is not None and item["frame"] != frame:
                continue
            return item
        return None

    def stage_graph(self) -> list[str]:
        stages = ["encode", "init"]
        stages.extend(f"denoise_{step:03d}" for step in range(self.steps))
        stages.extend(f"decode_frame_{frame:03d}" for frame in range(self.frames))
        stages.append("preview")
        return stages


def load_config_file(path: str | Path, base_dir: str | Path | None = None) -> tuple[dict[str, Any], Path]:
    config_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError("configuration file does not exist", path=str(config_path)) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("configuration file is not valid JSON", path=str(config_path), error=type(exc).__name__) from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuration must be a JSON object")
    resolved_base = Path(base_dir).expanduser().resolve() if base_dir is not None else Path.cwd().resolve()
    return raw, resolved_base


def validate_config(raw: Mapping[str, Any], base_dir: str | Path | None = None) -> ValidatedConfig:
    if not isinstance(raw, Mapping):
        raise ConfigError("configuration must be an object")
    work = copy.deepcopy(dict(raw))
    base = Path(base_dir).expanduser().resolve() if base_dir is not None else Path.cwd().resolve()
    _validate_required(work)

    config_version = _int_field(work, "config_version", minimum=1)
    if config_version != 1:
        raise ConfigError("unsupported config_version", config_version=config_version)
    mode = _str_field(work, "mode", nonempty=True)
    if mode not in SUPPORTED_MODES:
        raise ConfigError("mode must be t2v or i2v", mode=mode)
    prompt = _str_field(work, "prompt", nonempty=True)
    if not prompt.strip():
        raise ConfigError("prompt must not be empty")
    seed = _int_field(work, "seed", minimum=0, maximum=2**32 - 1)
    steps = _int_field(work, "steps", minimum=1, maximum=256)
    guidance = _float_field(work, "guidance")
    if guidance < 0:
        raise ConfigError("guidance must be non-negative", guidance=guidance)
    frames = _int_field(work, "frames", minimum=1, maximum=256)
    latent_shape = _shape_field(work, "latent_shape", length=3, minimum=1)
    output_size = _shape_field(work, "output_size", length=2, minimum=1)
    embed_dim = _int_field(work, "embed_dim", minimum=4, maximum=4096)
    max_workers = _int_field(work, "max_workers", minimum=1, maximum=64)
    preview_size = _shape_field(work, "preview_size", length=2, minimum=1)
    preview_budget_ms = _int_field(work, "preview_budget_ms", minimum=0, maximum=600000)
    stage_delay_ms = _int_field(work, "stage_delay_ms", minimum=0, maximum=600000) if "stage_delay_ms" in work else 0
    output_dir = _path_field(work, "output_dir", base)
    vocab_path = _path_field(work, "vocab_path", base)
    noise_table_path = _path_field(work, "noise_table_path", base)
    _ensure_output_writable(output_dir)

    vocab, vocab_fingerprint = _load_vocab(vocab_path)
    tokens = tokenize_prompt(prompt, set(vocab))
    if not tokens:
        raise ConfigError("prompt does not contain any vocabulary tokens", prompt=prompt)
    noise_table, noise_fingerprint = _load_noise_table(noise_table_path, steps, latent_shape)

    init_image: InitImage | None = None
    replacement_steps = 0
    if mode == "i2v":
        if "init_image_path" not in work or work.get("init_image_path") is None:
            raise InitImageError("i2v mode requires init_image_path")
        if isinstance(work.get("init_image_path"), str) and not work["init_image_path"].strip():
            raise InitImageError("i2v init_image_path must not be empty")
        init_path = _path_field(work, "init_image_path", base)
        init_image = _load_init_image(init_path, output_size)
        replacement_steps = _int_field(work, "i2v_replacement_steps", minimum=0, maximum=steps)
    elif work.get("i2v_replacement_steps") not in (None, 0):
        raise ConfigError("i2v_replacement_steps is only valid in i2v mode")

    patch = _validate_patch(work.get("patch"), steps, frames, latent_shape)
    injections = _validate_injections(work.get("inject_failures"), steps, frames)

    semantic = {
        "config_version": config_version,
        "mode": mode,
        "prompt": prompt,
        "tokens": tokens,
        "seed": seed,
        "steps": steps,
        "guidance": guidance,
        "frames": frames,
        "latent_shape": latent_shape,
        "output_size": output_size,
        "embed_dim": embed_dim,
        "max_workers": max_workers,
        "stage_delay_ms": stage_delay_ms,
        "preview_size": preview_size,
        "vocab_fingerprint": vocab_fingerprint,
        "noise_fingerprint": noise_fingerprint,
        "init_image_fingerprint": None if init_image is None else init_image.fingerprint,
        "i2v_replacement_steps": replacement_steps,
        "patch_fingerprint": None if patch is None else patch.fingerprint,
        "inject_failures": list(injections),
    }
    config_fingerprint = sha256_json(semantic)

    return ValidatedConfig(
        raw=work,
        base_dir=base,
        config_version=config_version,
        mode=mode,
        prompt=prompt,
        seed=seed,
        steps=steps,
        guidance=guidance,
        frames=frames,
        latent_shape=latent_shape,
        output_size=output_size,
        embed_dim=embed_dim,
        max_workers=max_workers,
        preview_size=preview_size,
        preview_budget_ms=preview_budget_ms,
        stage_delay_ms=stage_delay_ms,
        output_dir=output_dir,
        vocab_path=vocab_path,
        vocab=vocab,
        vocab_fingerprint=vocab_fingerprint,
        noise_table_path=noise_table_path,
        noise_table=noise_table,
        noise_fingerprint=noise_fingerprint,
        init_image=init_image,
        i2v_replacement_steps=replacement_steps,
        patch=patch,
        inject_failures=injections,
        config_fingerprint=config_fingerprint,
        tokens=tokens,
    )


def tokenize_prompt(prompt: str, vocabulary: set[str]) -> tuple[str, ...]:
    tokens = re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", prompt.lower())
    unknown = sorted(set(token for token in tokens if token not in vocabulary))
    if unknown:
        raise ConfigError("prompt contains tokens outside the restricted vocabulary", unknown_tokens=unknown)
    return tuple(tokens)


def _validate_required(raw: Mapping[str, Any]) -> None:
    required = [
        "config_version",
        "mode",
        "prompt",
        "seed",
        "steps",
        "guidance",
        "frames",
        "latent_shape",
        "output_size",
        "embed_dim",
        "vocab_path",
        "noise_table_path",
        "max_workers",
        "preview_size",
        "preview_budget_ms",
        "output_dir",
    ]
    missing = [key for key in required if key not in raw]
    if missing:
        raise ConfigError("configuration is missing required fields", missing=missing)


def _int_field(raw: Mapping[str, Any], key: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer", field=key, actual_type=type(value).__name__)
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} is below the allowed range", field=key, value=value, minimum=minimum)
    if maximum is not None and value > maximum:
        raise ConfigError(f"{key} is above the allowed range", field=key, value=value, maximum=maximum)
    return value


def _float_field(raw: Mapping[str, Any], key: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a number", field=key, actual_type=type(value).__name__)
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f"{key} must be finite", field=key, value=str(value))
    return result


def _str_field(raw: Mapping[str, Any], key: str, *, nonempty: bool = False) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string", field=key, actual_type=type(value).__name__)
    if nonempty and not value.strip():
        raise ConfigError(f"{key} must not be empty", field=key)
    return value


def _shape_field(raw: Mapping[str, Any], key: str, *, length: int, minimum: int) -> tuple[int, ...]:
    value = raw.get(key)
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ConfigError(f"{key} must be a list of {length} integers", field=key)
    result: list[int] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int):
            raise ConfigError(f"{key}[{index}] must be an integer", field=key, index=index)
        if item < minimum:
            raise ConfigError(f"{key}[{index}] must be at least {minimum}", field=key, index=index, value=item)
        result.append(item)
    return tuple(result)


def _path_field(raw: Mapping[str, Any], key: str, base: Path) -> Path:
    value = _str_field(raw, key, nonempty=True)
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _ensure_output_writable(output_dir: Path) -> None:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        descriptor, probe = tempfile.mkstemp(prefix=".write-probe-", dir=output_dir)
        os.close(descriptor)
        os.unlink(probe)
    except OSError as exc:
        raise ConfigError("output_dir is not writable", output_dir=str(output_dir), error=type(exc).__name__) from exc


def _load_vocab(path: Path) -> tuple[tuple[str, ...], str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError("vocabulary file does not exist", path=str(path)) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("vocabulary file is not valid JSON", path=str(path), error=type(exc).__name__) from exc
    words: Any
    if isinstance(payload, dict):
        words = payload.get("words")
    else:
        words = payload
    if not isinstance(words, list) or not words or not all(isinstance(word, str) and word for word in words):
        raise ConfigError("vocabulary must contain a non-empty list of words", path=str(path))
    normalized = [word.strip().lower() for word in words]
    if any(not re.fullmatch(r"[a-z0-9]+(?:'[a-z0-9]+)?", word) for word in normalized):
        raise ConfigError("vocabulary words must be lowercase ASCII tokens", path=str(path))
    if len(set(normalized)) != len(normalized):
        raise ConfigError("vocabulary contains duplicate words", path=str(path))
    return tuple(normalized), sha256_json(sorted(normalized))


def _load_noise_table(path: Path, steps: int, shape: tuple[int, ...]) -> tuple[np.ndarray, str]:
    try:
        table = np.load(path, allow_pickle=False)
    except FileNotFoundError as exc:
        raise NoiseMismatchError("noise table does not exist", path=str(path)) from exc
    except (OSError, ValueError) as exc:
        raise NoiseMismatchError("noise table cannot be loaded", path=str(path), error=type(exc).__name__) from exc
    if table.ndim != 4 or table.shape[0] != steps or tuple(table.shape[1:]) != shape:
        raise NoiseMismatchError(
            "noise table length or latent shape does not match configuration",
            path=str(path),
            actual_shape=list(table.shape),
            expected_shape=[steps, *shape],
        )
    table = np.ascontiguousarray(table, dtype=np.float64)
    if not np.all(np.isfinite(table)):
        raise NoiseMismatchError("noise table contains non-finite values", path=str(path))
    return table, array_fingerprint(table)


def _load_init_image(path: Path, output_size: tuple[int, int]) -> InitImage:
    try:
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            pixels = np.asarray(rgb, dtype=np.uint8).copy()
    except FileNotFoundError as exc:
        raise InitImageError("initial image does not exist", path=str(path)) from exc
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise InitImageError("initial image cannot be decoded", path=str(path), error=type(exc).__name__) from exc
    expected = (int(output_size[0]), int(output_size[1]))
    if rgb.size != expected:
        raise InitImageError(
            "initial image dimensions do not match output_size",
            path=str(path),
            actual_size=list(rgb.size),
            expected_size=list(expected),
        )
    return InitImage(path=path, pixels=np.ascontiguousarray(pixels), fingerprint=array_fingerprint(pixels))


def _validate_patch(
    value: Any,
    steps: int,
    frames: int,
    latent_shape: tuple[int, int, int],
) -> PatchSpec | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ConfigError("patch must be an object or null")
    if "step" not in value or "x" not in value or "y" not in value or "values" not in value:
        raise ConfigError("patch requires step, x, y and values", fields=sorted(value.keys()))
    step = _int_field(value, "step")
    if step < 0 or step >= steps:
        raise PatchRangeError("patch step is outside the configured denoise range", step=step, steps=steps)
    frame_value = value.get("frame")
    if frame_value is not None:
        if isinstance(frame_value, bool) or not isinstance(frame_value, int):
            raise ConfigError("patch.frame must be an integer or null", actual_type=type(frame_value).__name__)
        if frame_value < 0 or frame_value >= frames:
            raise PatchRangeError("patch frame is outside the configured frame range", frame=frame_value, frames=frames)
        frame: int | None = frame_value
    else:
        frame = None
    x = _int_field(value, "x")
    y = _int_field(value, "y")
    if x < 0 or y < 0:
        raise PatchRangeError("patch coordinates must be non-negative", x=x, y=y)
    try:
        array = np.asarray(value["values"], dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ConfigError("patch values must be a numeric 3-D array", error=type(exc).__name__) from exc
    if array.ndim != 3 or array.shape[2] != latent_shape[2] or array.shape[0] < 1 or array.shape[1] < 1:
        raise ConfigError(
            "patch values must have shape [height, width, latent_channels]",
            actual_shape=list(array.shape),
            expected_channels=latent_shape[2],
        )
    if not np.all(np.isfinite(array)):
        raise ConfigError("patch values must be finite")
    height, width = int(array.shape[0]), int(array.shape[1])
    if x + width > latent_shape[1] or y + height > latent_shape[0]:
        raise PatchRangeError(
            "patch rectangle is outside the latent bounds",
            x=x,
            y=y,
            width=width,
            height=height,
            latent_width=latent_shape[1],
            latent_height=latent_shape[0],
        )
    array = np.ascontiguousarray(array, dtype=np.float64)
    rect = {"x": x, "y": y, "width": width, "height": height}
    fingerprint = sha256_json({"step": step, "frame": frame, **rect, "values": array_fingerprint(array)})
    return PatchSpec(step=step, frame=frame, x=x, y=y, values=array, fingerprint=fingerprint, rect=rect)


def _validate_injections(value: Any, steps: int, frames: int) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError("inject_failures must be a list or null")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ConfigError("inject_failures entries must be objects", index=index)
        stage = item.get("stage")
        if stage not in SUPPORTED_INJECTION_STAGES:
            raise ConfigError("inject_failures stage is not supported", index=index, stage=stage)
        step = item.get("step")
        frame = item.get("frame")
        if step is not None:
            if isinstance(step, bool) or not isinstance(step, int) or step < 0 or step >= steps:
                raise ConfigError("inject_failures step is out of range", index=index, step=step)
        if frame is not None:
            if isinstance(frame, bool) or not isinstance(frame, int) or frame < 0 or frame >= frames:
                raise ConfigError("inject_failures frame is out of range", index=index, frame=frame)
        error_type = item.get("error_type", "InjectedStageError")
        if not isinstance(error_type, str) or not error_type:
            raise ConfigError("inject_failures error_type must be a string", index=index)
        result.append({"stage": stage, "step": step, "frame": frame, "error_type": error_type})
    return tuple(result)


def config_signature(raw: Mapping[str, Any]) -> str:
    # Invalid configurations may intentionally contain NaN/Infinity. They still
    # need a stable report signature even though they are never rendered.
    return sha256_bytes(
        json.dumps(json_safe(raw), sort_keys=True, separators=(",", ":"), allow_nan=True).encode("utf-8")
    )


def normalized_config_for_output(config: ValidatedConfig) -> dict[str, Any]:
    return {
        "mode": config.mode,
        "prompt": config.prompt,
        "tokens": list(config.tokens),
        "seed": config.seed,
        "steps": config.steps,
        "guidance": config.guidance,
        "frames": config.frames,
        "latent_shape": list(config.latent_shape),
        "output_size": list(config.output_size),
        "embed_dim": config.embed_dim,
        "max_workers": config.max_workers,
        "preview_size": list(config.preview_size),
        "preview_budget_ms": config.preview_budget_ms,
        "stage_delay_ms": config.stage_delay_ms,
        "i2v_replacement_steps": config.i2v_replacement_steps,
        "config_fingerprint": config.config_fingerprint,
        "patch_fingerprint": None if config.patch is None else config.patch.fingerprint,
        "inject_failures": list(config.inject_failures),
    }


def command_merge(raw: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(raw))
    for key, value in updates.items():
        merged[key] = copy.deepcopy(value)
    return merged
