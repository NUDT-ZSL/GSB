from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import EffectiveConfig
from .errors import EngineError
from .hashing import hash_json


VALID_DTYPES = {"float64", "float32", "fixed"}
VALID_ROUNDING = {"nearest_even", "nearest_away", "floor", "ceil", "trunc"}
DTYPE_ALIASES = {
    "fp64": "float64",
    "double": "float64",
    "fp32": "float32",
    "single": "float32",
    "int": "fixed",
    "integer": "fixed",
    "quantized": "fixed",
}
ROUNDING_ALIASES = {
    "nearest": "nearest_even",
    "round": "nearest_even",
    "half_even": "nearest_even",
    "away": "nearest_away",
    "truncate": "trunc",
}


def _as_pair(value: Any, field_name: str, task_id: str | None) -> tuple[int, int]:
    if isinstance(value, str) and "x" in value.lower():
        parts = value.lower().split("x")
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        parts = []
    if len(parts) != 2:
        raise EngineError(
            "E_TASK_INVALID",
            f"{field_name} must contain two positive integers",
            task_id=task_id,
            details={field_name: value},
        )
    try:
        result = (int(parts[0]), int(parts[1]))
    except (TypeError, ValueError) as exc:
        raise EngineError(
            "E_TASK_INVALID",
            f"{field_name} must contain two positive integers",
            task_id=task_id,
            details={field_name: value},
        ) from exc
    if result[0] <= 0 or result[1] <= 0:
        raise EngineError(
            "E_TASK_INVALID",
            f"{field_name} must contain two positive integers",
            task_id=task_id,
            details={field_name: value},
        )
    return result


def _as_shape(value: Any, field_name: str, task_id: str | None) -> tuple[int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise EngineError(
            "E_LATENT_SHAPE_MISMATCH",
            f"{field_name} must be [channels, height, width]",
            task_id=task_id,
            details={field_name: value},
        )
    try:
        shape = tuple(int(v) for v in value)
    except (TypeError, ValueError) as exc:
        raise EngineError(
            "E_LATENT_SHAPE_MISMATCH",
            f"{field_name} must be [channels, height, width]",
            task_id=task_id,
            details={field_name: value},
        ) from exc
    if any(v <= 0 for v in shape):
        raise EngineError(
            "E_LATENT_SHAPE_MISMATCH",
            f"{field_name} dimensions must be positive",
            task_id=task_id,
            details={field_name: value},
        )
    return shape  # type: ignore[return-value]


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    prompt: str
    prompt_seed: int
    latent_shape: tuple[int, int, int]
    block_grid: tuple[int, int]
    sampler_steps: int
    noise_scale: float
    quant_scale: int
    rounding_mode: str
    dtype_profile: str
    revision: int = 1
    depends_on: tuple[str, ...] = ()
    priority: int = 0
    latent_file: str | None = None
    noise_shape: tuple[int, int, int] | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        *,
        base_dir: str | Path | None = None,
    ) -> "TaskSpec":
        if not isinstance(payload, dict):
            raise EngineError("E_TASK_INVALID", "task entry must be a JSON object")
        task_id_raw = payload.get("task_id")
        if not isinstance(task_id_raw, str) or not task_id_raw.strip():
            raise EngineError(
                "E_TASK_INVALID",
                "task_id must be a non-empty string",
                details={"task_id": task_id_raw},
            )
        task_id = task_id_raw.strip()
        if "/" in task_id or "\\" in task_id or task_id in {".", ".."}:
            raise EngineError(
                "E_TASK_INVALID",
                "task_id must not contain path separators",
                task_id=task_id,
            )
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            raise EngineError("E_TASK_INVALID", "prompt must be a string", task_id=task_id)
        prompt_seed = payload.get("prompt_seed")
        if isinstance(prompt_seed, bool) or not isinstance(prompt_seed, int) or prompt_seed < 0:
            raise EngineError(
                "E_TASK_INVALID",
                "prompt_seed must be a non-negative integer",
                task_id=task_id,
                details={"prompt_seed": prompt_seed},
            )
        latent_shape = _as_shape(payload.get("latent_shape"), "latent_shape", task_id)
        block_grid = _as_pair(payload.get("block_grid"), "block_grid", task_id)
        sampler_steps = payload.get("sampler_steps")
        if (
            isinstance(sampler_steps, bool)
            or not isinstance(sampler_steps, int)
            or sampler_steps <= 0
        ):
            raise EngineError(
                "E_SAMPLER_STEPS_INVALID",
                "sampler_steps must be a positive integer",
                task_id=task_id,
                details={"sampler_steps": sampler_steps},
            )
        noise_scale = payload.get("noise_scale")
        if isinstance(noise_scale, bool) or not isinstance(noise_scale, (int, float)):
            raise EngineError(
                "E_TASK_INVALID",
                "noise_scale must be numeric",
                task_id=task_id,
                details={"noise_scale": noise_scale},
            )
        quant_scale = payload.get("quant_scale")
        quant_valid = (
            not isinstance(quant_scale, bool)
            and isinstance(quant_scale, (int, float))
            and float(quant_scale).is_integer()
            and quant_scale > 0
        )
        if not quant_valid:
            raise EngineError(
                "E_QUANT_SCALE_INVALID",
                "quant_scale must be a positive integer",
                task_id=task_id,
                details={"quant_scale": quant_scale},
            )
        rounding_mode = payload.get("rounding_mode")
        if isinstance(rounding_mode, str):
            rounding_mode = ROUNDING_ALIASES.get(rounding_mode.lower(), rounding_mode.lower())
        if rounding_mode not in VALID_ROUNDING:
            raise EngineError(
                "E_ROUNDING_MODE_INVALID",
                f"unsupported rounding_mode: {rounding_mode}",
                task_id=task_id,
                details={"rounding_mode": rounding_mode},
            )
        dtype_profile = payload.get("dtype_profile")
        if isinstance(dtype_profile, str):
            dtype_profile = DTYPE_ALIASES.get(dtype_profile.lower(), dtype_profile.lower())
        if dtype_profile not in VALID_DTYPES:
            raise EngineError(
                "E_DTYPE_PROFILE_INVALID",
                f"unsupported dtype_profile: {dtype_profile}",
                task_id=task_id,
                details={"dtype_profile": dtype_profile},
            )
        revision = payload.get("revision", 1)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise EngineError(
                "E_REVISION_INVALID",
                "revision must be a positive integer",
                task_id=task_id,
                details={"revision": revision},
            )
        depends_raw = payload.get("depends_on", [])
        if depends_raw is None:
            depends_raw = []
        if not isinstance(depends_raw, (list, tuple)) or any(
            not isinstance(v, str) or not v for v in depends_raw
        ):
            raise EngineError(
                "E_TASK_INVALID",
                "depends_on must be a list of task ids",
                task_id=task_id,
                details={"depends_on": depends_raw},
            )
        depends_on = tuple(sorted(set(depends_raw)))
        priority = payload.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise EngineError(
                "E_TASK_INVALID",
                "priority must be an integer",
                task_id=task_id,
                details={"priority": priority},
            )
        latent_file = payload.get("latent_file", payload.get("noise_file"))
        if latent_file is not None and not isinstance(latent_file, str):
            raise EngineError(
                "E_TASK_INVALID",
                "latent_file must be a string",
                task_id=task_id,
            )
        if latent_file is not None and base_dir is not None and not Path(latent_file).is_absolute():
            latent_file = str((Path(base_dir) / latent_file).resolve())

        noise_shape_raw = payload.get("noise_shape")
        noise_shape = (
            _as_shape(noise_shape_raw, "noise_shape", task_id)
            if noise_shape_raw is not None
            else None
        )
        if noise_shape is not None and noise_shape != latent_shape:
            raise EngineError(
                "E_LATENT_SHAPE_MISMATCH",
                "latent_shape and noise_shape differ",
                task_id=task_id,
                details={"latent_shape": list(latent_shape), "noise_shape": list(noise_shape)},
            )
        if latent_shape[1] % block_grid[0] != 0 or latent_shape[2] % block_grid[1] != 0:
            raise EngineError(
                "E_BLOCK_GRID_NOT_DIVISIBLE",
                "block_grid does not divide latent height and width",
                task_id=task_id,
                details={
                    "latent_shape": list(latent_shape),
                    "block_grid": list(block_grid),
                },
            )

        return cls(
            task_id=task_id,
            prompt=prompt,
            prompt_seed=int(prompt_seed),
            latent_shape=latent_shape,
            block_grid=block_grid,
            sampler_steps=int(sampler_steps),
            noise_scale=float(noise_scale),
            quant_scale=int(quant_scale),
            rounding_mode=str(rounding_mode),
            dtype_profile=str(dtype_profile),
            revision=int(revision),
            depends_on=depends_on,
            priority=int(priority),
            latent_file=latent_file,
            noise_shape=noise_shape,
            raw=dict(payload),
        )

    @property
    def channels(self) -> int:
        return self.latent_shape[0]

    @property
    def height(self) -> int:
        return self.latent_shape[1]

    @property
    def width(self) -> int:
        return self.latent_shape[2]

    @property
    def block_count(self) -> int:
        return self.block_grid[0] * self.block_grid[1]

    @property
    def denoise_iterations(self) -> int:
        return self.block_count * self.sampler_steps

    def computational_payload(self, config: EffectiveConfig) -> dict[str, Any]:
        normalized_prompt = " ".join(self.prompt.lower().split())
        return {
            "task_id": self.task_id,
            "revision": self.revision,
            "prompt": normalized_prompt,
            "prompt_seed": self.prompt_seed,
            "latent_shape": list(self.latent_shape),
            "block_grid": list(self.block_grid),
            "sampler_steps": self.sampler_steps,
            "noise_scale": self.noise_scale,
            "quant_scale": self.quant_scale,
            "rounding_mode": self.rounding_mode,
            "dtype_profile": self.dtype_profile,
            "config": config.compute_hash,
            "tolerance": config.tolerance,
        }

    def input_hash(self, config: EffectiveConfig) -> str:
        return hash_json(self.computational_payload(config))

    def public_dict(self) -> dict[str, Any]:
        return dict(self.raw)

    def storage_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "prompt": self.prompt,
            "prompt_seed": self.prompt_seed,
            "latent_shape": list(self.latent_shape),
            "block_grid": list(self.block_grid),
            "sampler_steps": self.sampler_steps,
            "noise_scale": self.noise_scale,
            "quant_scale": self.quant_scale,
            "rounding_mode": self.rounding_mode,
            "dtype_profile": self.dtype_profile,
            "revision": self.revision,
            "depends_on": list(self.depends_on),
            "priority": self.priority,
        }
        if self.latent_file is not None:
            payload["latent_file"] = self.latent_file
        if self.noise_shape is not None:
            payload["noise_shape"] = list(self.noise_shape)
        return payload


def parse_manifest_lines(lines: Iterable[str], *, base_dir: str | Path | None = None) -> list[TaskSpec]:
    tasks: list[TaskSpec] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EngineError(
                "E_TASK_JSON_INVALID",
                f"invalid JSON on line {line_number}: {exc}",
                details={"line": line_number},
            ) from exc
        tasks.append(TaskSpec.from_dict(payload, base_dir=base_dir))
    return tasks


def load_manifest(path: str | Path) -> list[TaskSpec]:
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise EngineError("E_TASKS_NOT_FOUND", f"task manifest not found: {manifest_path}")
    return parse_manifest_lines(
        manifest_path.read_text(encoding="utf-8").splitlines(),
        base_dir=manifest_path.parent,
    )


def compare_task_fields(old: TaskSpec, new: TaskSpec) -> set[str]:
    fields = (
        "prompt",
        "prompt_seed",
        "latent_shape",
        "block_grid",
        "sampler_steps",
        "noise_scale",
        "quant_scale",
        "rounding_mode",
        "dtype_profile",
    )
    changed: set[str] = set()
    for field_name in fields:
        old_value = getattr(old, field_name)
        new_value = getattr(new, field_name)
        if field_name == "prompt":
            old_value = " ".join(old_value.lower().split())
            new_value = " ".join(new_value.lower().split())
        if old_value != new_value:
            changed.add(field_name)
    return changed


def validate_manifest(
    tasks: list[TaskSpec],
    config: EffectiveConfig,
) -> dict[str, Any]:
    if not tasks:
        raise EngineError("E_EMPTY_MANIFEST", "task manifest contains no tasks")
    seen: dict[str, int] = {}
    task_by_id: dict[str, TaskSpec] = {}
    for index, task in enumerate(tasks):
        if task.task_id in seen:
            raise EngineError(
                "E_DUPLICATE_TASK_ID",
                "duplicate task_id in manifest",
                task_id=task.task_id,
                details={"first_index": seen[task.task_id], "duplicate_index": index},
            )
        seen[task.task_id] = index
        task_by_id[task.task_id] = task
        task.input_hash(config)

    graph: dict[str, list[str]] = {task.task_id: list(task.depends_on) for task in tasks}
    for task in tasks:
        for dependency in task.depends_on:
            if dependency not in task_by_id:
                raise EngineError(
                    "E_DEPENDENCY_MISSING",
                    f"dependency does not exist: {dependency}",
                    task_id=task.task_id,
                    details={"depends_on": dependency},
                )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        if node in visiting:
            cycle = stack[stack.index(node) :] + [node]
            raise EngineError(
                "E_DEPENDENCY_CYCLE",
                "dependency cycle detected",
                task_id=node,
                details={"cycle": cycle},
            )
        if node in visited:
            return
        visiting.add(node)
        stack.append(node)
        for child in graph.get(node, []):
            visit(child, stack)
        stack.pop()
        visiting.remove(node)
        visited.add(node)

    for task_id in graph:
        visit(task_id, [])
    return {"task_by_id": task_by_id, "order": [task.task_id for task in tasks]}

