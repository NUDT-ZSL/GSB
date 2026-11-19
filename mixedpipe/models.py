"""Task manifest and configuration data models."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from .canonical import content_checksum
from .errors import (
    BusinessError,
    E_COMPUTE_BUDGET,
    E_CONFIG_INVALID,
    E_MANIFEST_INVALID,
    E_MEMORY_BUDGET,
    E_SLOT_BUDGET,
)


JOB_KINDS = {"train", "infer"}
FAULT_STAGES = {"load", "forward", "backward", "update", "score"}


def _positive_int(value: Any, field_name: str, job_id: str | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        location = f" job_id={job_id}" if job_id else ""
        raise BusinessError(
            E_MANIFEST_INVALID,
            f"{field_name} must be a positive integer{location}",
            stage="manifest_validate",
            details={"field": field_name, "job_id": job_id, "value": value},
        )
    return value


@dataclass(frozen=True)
class Task:
    job_id: str
    job_kind: str
    model_tag: str
    batch_size: int
    seq_len: int
    feature_dim: int
    seed: int
    priority: int
    steps: int | None = None
    warmup: int | None = None
    fault: str | None = None

    @property
    def iterations(self) -> int:
        if self.job_kind == "train":
            assert self.steps is not None
            return self.steps
        assert self.warmup is not None
        return self.warmup

    @property
    def iteration_field(self) -> str:
        return "steps" if self.job_kind == "train" else "warmup"

    @property
    def parameter_count(self) -> int:
        return self.feature_dim * self.feature_dim + self.feature_dim

    def with_effective(self, batch_size: int, iterations: int) -> "Task":
        if self.job_kind == "train":
            return replace(self, batch_size=batch_size, steps=iterations)
        return replace(self, batch_size=batch_size, warmup=iterations)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data["fault"] is None:
            data.pop("fault")
        if data["steps"] is None:
            data.pop("steps")
        if data["warmup"] is None:
            data.pop("warmup")
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any], line_number: int | None = None) -> "Task":
        if not isinstance(raw, dict):
            raise BusinessError(
                E_MANIFEST_INVALID,
                "Each JSONL line must be a JSON object",
                stage="manifest_parse",
                details={"line_number": line_number},
            )

        def string_field(name: str) -> str:
            value = raw.get(name)
            if not isinstance(value, str) or not value.strip():
                raise BusinessError(
                    E_MANIFEST_INVALID,
                    f"{name} must be a non-empty string",
                    stage="manifest_validate",
                    details={"line_number": line_number, "field": name},
                )
            return value.strip()

        job_id = string_field("job_id")
        job_kind = string_field("job_kind")
        if job_kind not in JOB_KINDS:
            raise BusinessError(
                E_MANIFEST_INVALID,
                f"job_kind must be one of {sorted(JOB_KINDS)}",
                stage="manifest_validate",
                details={"job_id": job_id, "field": "job_kind", "value": job_kind},
            )

        model_tag = string_field("model_tag")
        batch_size = _positive_int(raw.get("batch_size"), "batch_size", job_id)
        seq_len = _positive_int(raw.get("seq_len"), "seq_len", job_id)
        feature_dim = _positive_int(raw.get("feature_dim"), "feature_dim", job_id)
        seed = raw.get("seed")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise BusinessError(
                E_MANIFEST_INVALID,
                "seed must be a non-negative integer",
                stage="manifest_validate",
                details={"job_id": job_id, "field": "seed", "value": seed},
            )
        priority = raw.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise BusinessError(
                E_MANIFEST_INVALID,
                "priority must be an integer",
                stage="manifest_validate",
                details={"job_id": job_id, "field": "priority", "value": priority},
            )

        steps: int | None = None
        warmup: int | None = None
        if job_kind == "train":
            if "warmup" in raw:
                raise BusinessError(
                    E_MANIFEST_INVALID,
                    "train jobs use steps, not warmup",
                    stage="manifest_validate",
                    details={"job_id": job_id, "field": "warmup"},
                )
            steps = _positive_int(raw.get("steps"), "steps", job_id)
        else:
            if "steps" in raw:
                raise BusinessError(
                    E_MANIFEST_INVALID,
                    "infer jobs use warmup, not steps",
                    stage="manifest_validate",
                    details={"job_id": job_id, "field": "steps"},
                )
            warmup = _positive_int(raw.get("warmup"), "warmup", job_id)

        fault = raw.get("fault")
        if fault is not None:
            if not isinstance(fault, str) or fault not in FAULT_STAGES:
                raise BusinessError(
                    E_MANIFEST_INVALID,
                    f"fault must be one of {sorted(FAULT_STAGES)}",
                    stage="manifest_validate",
                    details={"job_id": job_id, "field": "fault", "value": fault},
                )
            if job_kind == "infer" and fault in {"backward", "update"}:
                raise BusinessError(
                    E_MANIFEST_INVALID,
                    "infer jobs cannot inject backward/update faults",
                    stage="manifest_validate",
                    details={"job_id": job_id, "field": "fault", "value": fault},
                )

        return cls(
            job_id=job_id,
            job_kind=job_kind,
            model_tag=model_tag,
            batch_size=batch_size,
            seq_len=seq_len,
            feature_dim=feature_dim,
            seed=seed,
            priority=priority,
            steps=steps,
            warmup=warmup,
            fault=fault,
        )


def validate_tasks(tasks: Iterable[Task]) -> list[Task]:
    task_list = list(tasks)
    seen_ids: set[str] = set()
    tag_dims: dict[str, int] = {}
    for task in task_list:
        if task.job_id in seen_ids:
            raise BusinessError(
                E_MANIFEST_INVALID,
                "job_id values must be unique",
                stage="manifest_validate",
                details={"job_id": task.job_id},
            )
        seen_ids.add(task.job_id)
        previous_dim = tag_dims.get(task.model_tag)
        if previous_dim is not None and previous_dim != task.feature_dim:
            raise BusinessError(
                E_MANIFEST_INVALID,
                "one model_tag must use one feature_dim",
                stage="manifest_validate",
                details={
                    "model_tag": task.model_tag,
                    "feature_dims": [previous_dim, task.feature_dim],
                },
            )
        tag_dims[task.model_tag] = task.feature_dim
    return task_list


def load_manifest(path: str | Path) -> list[Task]:
    manifest_path = Path(path)
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BusinessError(
            E_MANIFEST_INVALID,
            "manifest cannot be read",
            stage="manifest_load",
            details={"path": str(manifest_path), "error": str(exc)},
        ) from exc

    tasks: list[Task] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BusinessError(
                E_MANIFEST_INVALID,
                "invalid JSONL line",
                stage="manifest_parse",
                details={
                    "line_number": line_number,
                    "column": exc.colno,
                    "error": exc.msg,
                },
            ) from exc
        tasks.append(Task.from_dict(raw, line_number=line_number))
    if not tasks:
        raise BusinessError(
            E_MANIFEST_INVALID,
            "manifest contains no jobs",
            stage="manifest_validate",
            details={"path": str(manifest_path)},
        )
    return validate_tasks(tasks)


def manifest_checksum(tasks: Iterable[Task]) -> str:
    return content_checksum([task.to_dict() for task in tasks])


@dataclass(frozen=True)
class PipelineConfig:
    max_slots: int = 3
    compute_budget: int = 10_000_000
    memory_budget_mb: float = 8.0
    bytes_per_scalar: int = 8
    activation_copies: int = 3
    gradient_buffer_copies: int = 1
    learning_rate: float = 0.03
    degradation_enabled: bool = True
    degradation_priority_floor: int = 0
    min_batch_size: int = 1
    min_iterations: int = 1
    max_degrade_candidates: int = 24
    equivalence_atol: float = 0.0
    equivalence_rtol: float = 0.0
    rejection_code_precedence: tuple[str, ...] = (
        E_SLOT_BUDGET,
        E_COMPUTE_BUDGET,
        E_MEMORY_BUDGET,
    )

    def validate(self) -> None:
        if isinstance(self.max_slots, bool) or self.max_slots < 0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "max_slots must be zero or greater",
                stage="config_validate",
                details={"field": "max_slots", "value": self.max_slots},
            )
        if self.compute_budget <= 0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "compute_budget must be positive",
                stage="config_validate",
                details={"field": "compute_budget", "value": self.compute_budget},
            )
        if self.memory_budget_mb <= 0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "memory_budget_mb must be positive",
                stage="config_validate",
                details={"field": "memory_budget_mb", "value": self.memory_budget_mb},
            )
        if self.bytes_per_scalar not in {2, 4, 8}:
            raise BusinessError(
                E_CONFIG_INVALID,
                "bytes_per_scalar must be 2, 4 or 8",
                stage="config_validate",
                details={"field": "bytes_per_scalar", "value": self.bytes_per_scalar},
            )
        if self.activation_copies < 1 or self.gradient_buffer_copies < 1:
            raise BusinessError(
                E_CONFIG_INVALID,
                "buffer copy counts must be at least 1",
                stage="config_validate",
            )
        if self.min_batch_size < 1 or self.min_iterations < 1:
            raise BusinessError(
                E_CONFIG_INVALID,
                "minimum batch and iteration values must be at least 1",
                stage="config_validate",
            )
        if self.equivalence_atol < 0 or self.equivalence_rtol < 0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "equivalence tolerances must be non-negative",
                stage="config_validate",
            )

    @property
    def memory_budget_bytes(self) -> int:
        return int(self.memory_budget_mb * 1024 * 1024)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["memory_budget_bytes"] = self.memory_budget_bytes
        payload["rejection_code_precedence"] = list(self.rejection_code_precedence)
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PipelineConfig":
        if not isinstance(raw, dict):
            raise BusinessError(
                E_CONFIG_INVALID,
                "config must be a JSON object",
                stage="config_parse",
            )
        allowed = {
            field_name
            for field_name in cls.__dataclass_fields__
            if field_name != "memory_budget_bytes"
        }
        values = {key: value for key, value in raw.items() if key in allowed}
        if "rejection_code_precedence" in values:
            values["rejection_code_precedence"] = tuple(values["rejection_code_precedence"])
        try:
            config = cls(**values)
        except (TypeError, ValueError) as exc:
            raise BusinessError(
                E_CONFIG_INVALID,
                "invalid config values",
                stage="config_parse",
                details={"error": str(exc)},
            ) from exc
        config.validate()
        return config


def load_config(path: str | Path) -> PipelineConfig:
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(
            E_CONFIG_INVALID,
            "config cannot be loaded",
            stage="config_load",
            details={"path": str(config_path), "error": str(exc)},
        ) from exc
    return PipelineConfig.from_dict(raw)
