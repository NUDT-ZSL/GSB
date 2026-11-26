"""Validated domain objects and parsers for tasks, plans and platform stats."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import (
    BUDGET_EXHAUSTED,
    CLOCK_REGRESSION,
    DUPLICATE_CANDIDATE,
    DUPLICATE_JOB_ID,
    DUPLICATE_STAT,
    INVALID_CACHE_LAYER,
    INVALID_CANDIDATE,
    INVALID_CLOCK,
    INVALID_COST,
    INVALID_COST_MODEL,
    INVALID_DEADLINE,
    INVALID_DTYPE,
    INVALID_HIT_RATE,
    INVALID_JSONL,
    INVALID_JOB_ID,
    INVALID_LAYOUT,
    INVALID_OP_KIND,
    INVALID_PLAN,
    INVALID_PLAN_VERSION,
    INVALID_PRECISION_FLAG,
    INVALID_PRIORITY,
    INVALID_REVISION,
    INVALID_SEED,
    INVALID_SHAPE,
    INVALID_STATS,
    INVALID_THREAD_OCCUPANCY,
    INVALID_THROUGHPUT,
    PLAN_CYCLE,
    SHAPE_OP_MISMATCH,
    UNKNOWN_JOB,
    BusinessError,
    fail,
)
from .util import (
    digest_obj,
    read_json_or_jsonl,
    require_bool,
    require_finite_number,
    require_int,
    require_list,
    require_nonempty_str,
    require_object,
)


OP_KINDS = ("matmul", "conv2d")
DTYPES = ("float16", "float32", "bfloat16", "int8")
CACHE_LAYERS = ("L1", "L2", "L3", "DRAM")
LAYOUTS = ("ROW_MAJOR", "COL_MAJOR", "NCHW", "NHWC")
LAYOUTS_BY_OP = {
    "matmul": ("ROW_MAJOR", "COL_MAJOR"),
    "conv2d": ("NCHW", "NHWC"),
}
PHASES = ("weight_sum", "activation")
DEFAULT_MAX_THREADS = 16


def _positive_shape(shape: Any, *, job_id: str) -> tuple[int, ...]:
    shape = require_list(
        shape,
        INVALID_SHAPE,
        "input shape must be a non-empty list of positive integers",
        job_id=job_id,
    )
    if not shape:
        fail(INVALID_SHAPE, "input shape must not be empty", job_id=job_id)
    result: list[int] = []
    for index, dim in enumerate(shape):
        value = require_int(
            dim,
            INVALID_SHAPE,
            "shape dimensions must be positive integers",
            job_id=job_id,
            dimension=index,
        )
        if value <= 0:
            fail(
                INVALID_SHAPE,
                "shape dimensions must be positive integers",
                job_id=job_id,
                dimension=index,
                value=value,
            )
        result.append(value)
    return tuple(result)


def _validate_op_shapes(
    op_kind: str, input_shapes: tuple[tuple[int, ...], ...], *, job_id: str
) -> None:
    if len(input_shapes) != 2:
        fail(
            SHAPE_OP_MISMATCH,
            f"{op_kind} requires exactly two input shapes",
            job_id=job_id,
            input_shapes=input_shapes,
        )
    lhs, rhs = input_shapes
    if op_kind == "matmul":
        if len(lhs) != 2 or len(rhs) != 2 or lhs[1] != rhs[0]:
            fail(
                SHAPE_OP_MISMATCH,
                "matmul requires A[m,k] and B[k,n] with matching reduction dimension",
                job_id=job_id,
                input_shapes=input_shapes,
            )
        return
    if op_kind == "conv2d":
        if (
            len(lhs) != 4
            or len(rhs) != 4
            or lhs[1] != rhs[1]
            or rhs[2] > lhs[2]
            or rhs[3] > lhs[3]
        ):
            fail(
                SHAPE_OP_MISMATCH,
                "conv2d requires input[N,C,H,W] and weight[O,C,KH,KW] with matching C and valid kernel",
                job_id=job_id,
                input_shapes=input_shapes,
            )
        return
    fail(INVALID_OP_KIND, "unsupported op_kind", job_id=job_id, op_kind=op_kind)


@dataclass(frozen=True)
class Task:
    job_id: str
    op_kind: str
    input_shapes: tuple[tuple[int, ...], ...]
    dtype: str
    layout: str
    seed: int
    revision: int
    logical_clock_ms: int
    precision_sensitive: bool

    @property
    def task_hash(self) -> str:
        return digest_obj(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "op_kind": self.op_kind,
            "input_shapes": [list(shape) for shape in self.input_shapes],
            "dtype": self.dtype,
            "layout": self.layout,
            "seed": self.seed,
            "revision": self.revision,
            "logical_clock_ms": self.logical_clock_ms,
            "precision_sensitive": self.precision_sensitive,
        }

    @classmethod
    def from_dict(cls, value: Any, *, line: int | None = None) -> "Task":
        job_id_hint = value.get("job_id", "<missing>") if isinstance(value, dict) else "<invalid>"
        obj = require_object(
            value,
            INVALID_JSONL,
            "task row must be a JSON object",
            line=line,
            job_id=job_id_hint,
        )
        job_id = require_nonempty_str(
            obj.get("job_id"),
            INVALID_JOB_ID,
            "job_id must be a non-empty string",
            line=line,
        )
        op_kind = require_nonempty_str(
            obj.get("op_kind"),
            INVALID_OP_KIND,
            "op_kind must be matmul or conv2d",
            job_id=job_id,
            line=line,
        )
        if op_kind not in OP_KINDS:
            fail(
                INVALID_OP_KIND,
                "op_kind must be matmul or conv2d",
                job_id=job_id,
                line=line,
                value=op_kind,
            )
        raw_shapes = require_list(
            obj.get("input_shapes"),
            INVALID_SHAPE,
            "input_shapes must be a list of shapes",
            job_id=job_id,
            line=line,
        )
        input_shapes = tuple(
            _positive_shape(shape, job_id=job_id) for shape in raw_shapes
        )
        _validate_op_shapes(op_kind, input_shapes, job_id=job_id)

        dtype = require_nonempty_str(
            obj.get("dtype"),
            INVALID_DTYPE,
            "dtype is not in the declared set",
            job_id=job_id,
            line=line,
        )
        if dtype not in DTYPES:
            fail(
                INVALID_DTYPE,
                "dtype is not in the declared set",
                job_id=job_id,
                line=line,
                value=dtype,
                allowed=list(DTYPES),
            )
        layout = require_nonempty_str(
            obj.get("layout"),
            INVALID_LAYOUT,
            "layout is not valid for this operator",
            job_id=job_id,
            line=line,
        )
        if layout not in LAYOUTS or layout not in LAYOUTS_BY_OP[op_kind]:
            fail(
                INVALID_LAYOUT,
                "layout is not valid for this operator",
                job_id=job_id,
                line=line,
                value=layout,
                allowed=list(LAYOUTS_BY_OP[op_kind]),
            )

        seed = require_int(
            obj.get("seed"),
            INVALID_SEED,
            "seed must be a non-negative integer",
            minimum=0,
            job_id=job_id,
            line=line,
        )
        revision = require_int(
            obj.get("revision"),
            INVALID_REVISION,
            "revision must be a positive integer",
            minimum=1,
            job_id=job_id,
            line=line,
        )
        logical_clock_ms = require_int(
            obj.get("logical_clock_ms"),
            INVALID_CLOCK,
            "logical_clock_ms must be a non-negative integer",
            minimum=0,
            job_id=job_id,
            line=line,
        )
        precision_sensitive = obj.get(
            "precision_sensitive", dtype in ("float16", "bfloat16", "int8")
        )
        precision_sensitive = require_bool(
            precision_sensitive,
            INVALID_PRECISION_FLAG,
            "precision_sensitive must be a boolean",
            job_id=job_id,
            line=line,
        )
        return cls(
            job_id=job_id,
            op_kind=op_kind,
            input_shapes=input_shapes,
            dtype=dtype,
            layout=layout,
            seed=seed,
            revision=revision,
            logical_clock_ms=logical_clock_ms,
            precision_sensitive=precision_sensitive,
        )


def parse_tasks_file(path: str | Path) -> list[Task]:
    records = read_json_or_jsonl(path, kind="jsonl")
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        fail(INVALID_JSONL, "task file must contain JSON objects", path=str(path))
    tasks: list[Task] = []
    seen: dict[str, int] = {}
    last_clock = -1
    for index, record in enumerate(records, start=1):
        task = Task.from_dict(record, line=index)
        if task.job_id in seen:
            fail(
                DUPLICATE_JOB_ID,
                "job_id must be unique within a task file",
                job_id=task.job_id,
                first_line=seen[task.job_id],
                duplicate_line=index,
            )
        seen[task.job_id] = index
        if task.logical_clock_ms < last_clock:
            fail(
                CLOCK_REGRESSION,
                "task logical_clock_ms must be monotonically non-decreasing",
                job_id=task.job_id,
                line=index,
                previous=last_clock,
                current=task.logical_clock_ms,
            )
        last_clock = task.logical_clock_ms
        tasks.append(task)
    if not tasks:
        fail(INVALID_JSONL, "task file contains no tasks", path=str(path))
    return tasks


@dataclass(frozen=True)
class PlatformStat:
    logical_clock_ms: int
    cache_layer: str
    hit_rate: float
    thread_occupancy: int
    throughput_cost: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_clock_ms": self.logical_clock_ms,
            "cache_layer": self.cache_layer,
            "hit_rate": self.hit_rate,
            "thread_occupancy": self.thread_occupancy,
            "throughput_cost": self.throughput_cost,
        }

    @property
    def stat_id(self) -> str:
        return digest_obj(self.to_dict())


@dataclass(frozen=True)
class PlatformStats:
    max_threads: int
    entries: tuple[PlatformStat, ...]
    source: str = ""

    def latest_by_layer(self) -> dict[str, PlatformStat]:
        result: dict[str, PlatformStat] = {}
        for entry in self.entries:
            current = result.get(entry.cache_layer)
            if current is None or entry.logical_clock_ms >= current.logical_clock_ms:
                result[entry.cache_layer] = entry
        return result

    def ordered_entries(self) -> list[PlatformStat]:
        return sorted(
            self.entries,
            key=lambda entry: (entry.logical_clock_ms, entry.cache_layer),
        )


def _parse_platform_records(
    path: str | Path, *, default_max_threads: int
) -> tuple[int, list[Any]]:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise BusinessError(
            INVALID_STATS,
            f"cannot read platform stats file: {source}",
            details={"path": str(source), "reason": str(exc)},
        ) from exc
    stripped = text.strip()
    if not stripped:
        fail(INVALID_STATS, "platform stats file is empty", path=str(source))

    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if parsed is not None:
        if isinstance(parsed, list):
            return default_max_threads, parsed
        if isinstance(parsed, dict):
            if "entries" in parsed or "records" in parsed or "rows" in parsed:
                records = parsed.get("entries", parsed.get("records", parsed.get("rows")))
                if not isinstance(records, list):
                    fail(INVALID_STATS, "platform stats entries must be a list", path=str(source))
                max_threads = parsed.get("max_threads", default_max_threads)
                max_threads = require_int(
                    max_threads,
                    INVALID_THREAD_OCCUPANCY,
                    "max_threads must be a positive integer",
                    minimum=1,
                    path=str(source),
                )
                return max_threads, records
            return default_max_threads, [parsed]

    records = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except (json.JSONDecodeError, ValueError) as exc:
            raise BusinessError(
                INVALID_STATS,
                f"invalid platform stats JSON on line {line_no}",
                details={"path": str(source), "line": line_no, "reason": str(exc)},
            ) from exc
    if not records:
        fail(INVALID_STATS, "platform stats file contains no records", path=str(source))
    return default_max_threads, records


def parse_platform_stats_file(
    path: str | Path, *, max_threads: int = DEFAULT_MAX_THREADS
) -> PlatformStats:
    declared_max, records = _parse_platform_records(path, default_max_threads=max_threads)
    if not isinstance(declared_max, int) or isinstance(declared_max, bool) or declared_max < 1:
        fail(INVALID_THREAD_OCCUPANCY, "max_threads must be a positive integer", path=str(path))
    entries: list[PlatformStat] = []
    seen: dict[tuple[int, str], int] = {}
    last_clock = -1
    for index, value in enumerate(records, start=1):
        obj = require_object(
            value,
            INVALID_STATS,
            "platform stats row must be a JSON object",
            line=index,
            path=str(path),
        )
        clock = require_int(
            obj.get("logical_clock_ms"),
            INVALID_CLOCK,
            "platform stats logical_clock_ms must be a non-negative integer",
            minimum=0,
            line=index,
            path=str(path),
        )
        if clock < last_clock:
            fail(
                CLOCK_REGRESSION,
                "platform stats logical_clock_ms must be monotonically non-decreasing",
                line=index,
                previous=last_clock,
                current=clock,
                path=str(path),
            )
        layer = require_nonempty_str(
            obj.get("cache_layer"),
            INVALID_CACHE_LAYER,
            "cache_layer is not in the declared set",
            line=index,
            path=str(path),
        )
        if layer not in CACHE_LAYERS:
            fail(
                INVALID_CACHE_LAYER,
                "cache_layer is not in the declared set",
                line=index,
                path=str(path),
                value=layer,
                allowed=list(CACHE_LAYERS),
            )
        hit_rate = require_finite_number(
            obj.get("hit_rate"),
            INVALID_HIT_RATE,
            "hit_rate must be a finite number in [0, 1]",
            minimum=0.0,
            maximum=1.0,
            line=index,
            path=str(path),
        )
        occupancy = require_int(
            obj.get("thread_occupancy"),
            INVALID_THREAD_OCCUPANCY,
            "thread_occupancy must be an integer in [0, max_threads]",
            minimum=0,
            line=index,
            path=str(path),
        )
        if occupancy > declared_max:
            fail(
                INVALID_THREAD_OCCUPANCY,
                "thread_occupancy exceeds declared max_threads",
                line=index,
                path=str(path),
                value=occupancy,
                max_threads=declared_max,
            )
        throughput = require_finite_number(
            obj.get("throughput_cost"),
            INVALID_THROUGHPUT,
            "throughput_cost must be a non-negative finite number",
            minimum=0.0,
            line=index,
            path=str(path),
        )
        key = (clock, layer)
        if key in seen:
            fail(
                DUPLICATE_STAT,
                "platform stats must be unique per (logical_clock_ms, cache_layer)",
                line=index,
                first_line=seen[key],
                duplicate_line=index,
                logical_clock_ms=clock,
                cache_layer=layer,
            )
        seen[key] = index
        last_clock = clock
        entries.append(
            PlatformStat(
                logical_clock_ms=clock,
                cache_layer=layer,
                hit_rate=hit_rate,
                thread_occupancy=occupancy,
                throughput_cost=throughput,
            )
        )
    if not entries:
        fail(INVALID_STATS, "platform stats file contains no entries", path=str(path))
    return PlatformStats(
        max_threads=declared_max,
        entries=tuple(entries),
        source=str(path),
    )


@dataclass(frozen=True)
class CostEstimate:
    cpu_instructions: float
    memory_bytes: float
    duration_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "cpu_instructions": self.cpu_instructions,
            "memory_bytes": self.memory_bytes,
        }
        if self.duration_ms is not None:
            result["duration_ms"] = self.duration_ms
        return result


@dataclass(frozen=True)
class CostModel:
    instruction_weight: float = 1.0
    memory_weight: float = 1.0
    cache_cpu_penalty: float = 0.35
    cache_memory_penalty: float = 2.0
    occupancy_penalty: float = 0.15
    logical_ms_per_cost_unit: float = 0.001

    def to_dict(self) -> dict[str, Any]:
        return {
            "instruction_weight": self.instruction_weight,
            "memory_weight": self.memory_weight,
            "cache_cpu_penalty": self.cache_cpu_penalty,
            "cache_memory_penalty": self.cache_memory_penalty,
            "occupancy_penalty": self.occupancy_penalty,
            "logical_ms_per_cost_unit": self.logical_ms_per_cost_unit,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "CostModel":
        if value is None:
            return cls()
        obj = require_object(
            value, INVALID_COST_MODEL, "cost_model must be an object"
        )
        defaults = cls()
        result = {}
        for key in (
            "instruction_weight",
            "memory_weight",
            "cache_cpu_penalty",
            "cache_memory_penalty",
            "occupancy_penalty",
            "logical_ms_per_cost_unit",
        ):
            default = getattr(defaults, key)
            result[key] = require_finite_number(
                obj.get(key, default),
                INVALID_COST_MODEL,
                f"cost_model.{key} must be non-negative and finite",
                minimum=0.0,
            )
        return cls(**result)


@dataclass(frozen=True)
class PlanJob:
    job_id: str
    priority: int
    deadline_clock_ms: float
    cost_estimates: dict[str, CostEstimate]
    dependencies: tuple[str, ...] = ()
    critical_path_id: str = ""
    precision_sensitive: bool | None = None
    threads_required: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "priority": self.priority,
            "deadline_clock_ms": self.deadline_clock_ms,
            "cost_estimates": {
                phase: self.cost_estimates[phase].to_dict() for phase in PHASES
            },
            "dependencies": list(self.dependencies),
            "critical_path_id": self.critical_path_id,
            "precision_sensitive": self.precision_sensitive,
            "threads_required": self.threads_required,
        }

    def merged(self, override: Any) -> "PlanJob":
        if override is None:
            return self
        obj = require_object(
            override,
            INVALID_CANDIDATE,
            "candidate job override must be an object",
            job_id=self.job_id,
        )
        data = self.to_dict()
        for key, value in obj.items():
            if key == "job_id":
                continue
            if key in ("dependencies", "precision_sensitive"):
                current = data[key]
                if key == "dependencies":
                    same = isinstance(value, list) and tuple(value) == tuple(current)
                else:
                    same = value == current
                if not same:
                    fail(
                        INVALID_CANDIDATE,
                        f"candidate cannot override semantic field: {key}",
                        job_id=self.job_id,
                    )
            if key not in data:
                fail(
                    INVALID_CANDIDATE,
                    f"unknown candidate job field: {key}",
                    job_id=self.job_id,
                )
            data[key] = value
        return _parse_plan_job(data)


def _parse_estimate(value: Any, *, job_id: str, phase: str) -> CostEstimate:
    obj = require_object(
        value,
        INVALID_COST,
        "cost estimate must be an object",
        job_id=job_id,
        phase=phase,
    )
    cpu = require_finite_number(
        obj.get("cpu_instructions"),
        INVALID_COST,
        "cpu_instructions must be a non-negative finite number",
        minimum=0.0,
        job_id=job_id,
        phase=phase,
    )
    memory = require_finite_number(
        obj.get("memory_bytes"),
        INVALID_COST,
        "memory_bytes must be a non-negative finite number",
        minimum=0.0,
        job_id=job_id,
        phase=phase,
    )
    duration = obj.get("duration_ms")
    if duration is not None:
        duration = require_finite_number(
            duration,
            INVALID_COST,
            "duration_ms must be a non-negative finite number",
            minimum=0.0,
            job_id=job_id,
            phase=phase,
        )
    return CostEstimate(
        cpu_instructions=cpu,
        memory_bytes=memory,
        duration_ms=duration,
    )


def _parse_cost_estimates(value: Any, *, job_id: str) -> dict[str, CostEstimate]:
    if isinstance(value, list):
        obj: dict[str, Any] = {}
        for item in value:
            entry = require_object(
                item,
                INVALID_COST,
                "cost_estimates list entries must be objects",
                job_id=job_id,
            )
            phase = entry.get("phase")
            if phase not in PHASES:
                fail(
                    INVALID_COST,
                    "cost estimate phase must be weight_sum or activation",
                    job_id=job_id,
                    phase=phase,
                )
            if phase in obj:
                fail(
                    INVALID_COST,
                    "duplicate cost estimate phase",
                    job_id=job_id,
                    phase=phase,
                )
            obj[phase] = entry
    else:
        obj = require_object(
            value,
            INVALID_COST,
            "cost_estimates must be an object keyed by phase",
            job_id=job_id,
        )
    estimates: dict[str, CostEstimate] = {}
    for phase in PHASES:
        if phase not in obj:
            fail(
                INVALID_COST,
                "missing required cost estimate phase",
                job_id=job_id,
                phase=phase,
            )
        estimates[phase] = _parse_estimate(obj[phase], job_id=job_id, phase=phase)
    return estimates


def _parse_plan_job(value: Any) -> PlanJob:
    obj = require_object(value, INVALID_PLAN, "plan job must be an object")
    job_id = require_nonempty_str(
        obj.get("job_id"),
        INVALID_JOB_ID,
        "plan job_id must be a non-empty string",
    )
    priority = require_int(
        obj.get("priority", 0),
        INVALID_PRIORITY,
        "priority must be a non-negative integer",
        minimum=0,
        job_id=job_id,
    )
    deadline = require_finite_number(
        obj.get("deadline_clock_ms"),
        INVALID_DEADLINE,
        "deadline_clock_ms must be a non-negative finite number",
        minimum=0.0,
        job_id=job_id,
    )
    estimates = _parse_cost_estimates(obj.get("cost_estimates"), job_id=job_id)
    raw_deps = obj.get("dependencies", [])
    raw_deps = require_list(
        raw_deps,
        INVALID_PLAN,
        "dependencies must be a list of job IDs",
        job_id=job_id,
    )
    deps: list[str] = []
    for dep in raw_deps:
        dep_id = require_nonempty_str(
            dep,
            INVALID_PLAN,
            "dependency job_id must be a non-empty string",
            job_id=job_id,
        )
        if dep_id == job_id:
            fail(
                PLAN_CYCLE,
                "a job cannot depend on itself",
                job_id=job_id,
            )
        if dep_id not in deps:
            deps.append(dep_id)
    critical_path_id = obj.get("critical_path_id", "")
    if critical_path_id is None:
        critical_path_id = ""
    critical_path_id = require_nonempty_str(
        critical_path_id,
        INVALID_PLAN,
        "critical_path_id must be a string",
        job_id=job_id,
    ) if critical_path_id != "" else ""
    precision_sensitive = obj.get("precision_sensitive")
    if precision_sensitive is not None:
        precision_sensitive = require_bool(
            precision_sensitive,
            INVALID_PRECISION_FLAG,
            "precision_sensitive must be a boolean or null",
            job_id=job_id,
        )
    threads_required = require_int(
        obj.get("threads_required", 1),
        INVALID_PLAN,
        "threads_required must be a positive integer",
        minimum=1,
        job_id=job_id,
    )
    return PlanJob(
        job_id=job_id,
        priority=priority,
        deadline_clock_ms=deadline,
        cost_estimates=estimates,
        dependencies=tuple(deps),
        critical_path_id=critical_path_id,
        precision_sensitive=precision_sensitive,
        threads_required=threads_required,
    )


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    job_order: tuple[str, ...]
    interleave_jobs: bool = False
    jobs: dict[str, PlanJob] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "job_order": list(self.job_order),
            "interleave_jobs": self.interleave_jobs,
            "jobs": {
                job_id: self.jobs[job_id].to_dict()
                for job_id in sorted(self.jobs)
            },
        }


@dataclass(frozen=True)
class Plan:
    plan_version: str
    global_seed: int
    candidates: tuple[Candidate, ...]
    jobs: dict[str, PlanJob]
    cost_model: CostModel
    base_logical_clock_ms: int
    logical_clock_step_ms: int
    max_threads: int
    critical_paths: dict[str, tuple[str, ...]]

    @property
    def plan_hash(self) -> str:
        return digest_obj(self.to_dict())

    def candidate(self, candidate_id: str | None = None) -> Candidate:
        if candidate_id is None:
            return sorted(self.candidates, key=lambda item: item.candidate_id)[0]
        for candidate in self.candidates:
            if candidate.candidate_id == candidate_id:
                return candidate
        fail(
            INVALID_CANDIDATE,
            "candidate_id does not exist in this plan",
            plan_version=self.plan_version,
            candidate_id=candidate_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_version": self.plan_version,
            "global_seed": self.global_seed,
            "base_logical_clock_ms": self.base_logical_clock_ms,
            "logical_clock_step_ms": self.logical_clock_step_ms,
            "max_threads": self.max_threads,
            "cost_model": self.cost_model.to_dict(),
            "jobs": {
                job_id: self.jobs[job_id].to_dict() for job_id in sorted(self.jobs)
            },
            "candidates": [
                candidate.to_dict()
                for candidate in sorted(
                    self.candidates, key=lambda item: item.candidate_id
                )
            ],
            "critical_paths": {
                key: list(value) for key, value in sorted(self.critical_paths.items())
            },
        }


def _detect_job_cycle(jobs: dict[str, PlanJob]) -> None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(job_id: str) -> None:
        mark = state.get(job_id, 0)
        if mark == 1:
            cycle_start = stack.index(job_id) if job_id in stack else 0
            cycle = stack[cycle_start:] + [job_id]
            fail(
                PLAN_CYCLE,
                "job dependency graph contains a cycle",
                cycle=cycle,
            )
        if mark == 2:
            return
        state[job_id] = 1
        stack.append(job_id)
        for dep in jobs[job_id].dependencies:
            if dep not in jobs:
                fail(
                    UNKNOWN_JOB,
                    "job dependency references an unknown job",
                    job_id=job_id,
                    dependency=dep,
                )
            visit(dep)
        stack.pop()
        state[job_id] = 2

    for job_id in sorted(jobs):
        visit(job_id)


def parse_plan_object(value: Any, *, source: str = "<memory>") -> Plan:
    obj = require_object(
        value,
        INVALID_PLAN,
        "plan file must contain one plan object",
        path=source,
    )
    plan_version = require_nonempty_str(
        obj.get("plan_version"),
        INVALID_PLAN_VERSION,
        "plan_version must be a non-empty string",
        path=source,
    )
    global_seed = require_int(
        obj.get("global_seed"),
        INVALID_SEED,
        "global_seed must be a non-negative integer",
        minimum=0,
        plan_version=plan_version,
    )
    base_clock = require_int(
        obj.get("base_logical_clock_ms", 0),
        INVALID_CLOCK,
        "base_logical_clock_ms must be a non-negative integer",
        minimum=0,
        plan_version=plan_version,
    )
    clock_step = require_int(
        obj.get("logical_clock_step_ms", 10),
        INVALID_CLOCK,
        "logical_clock_step_ms must be a positive integer",
        minimum=1,
        plan_version=plan_version,
    )
    max_threads = require_int(
        obj.get("max_threads", DEFAULT_MAX_THREADS),
        INVALID_THREAD_OCCUPANCY,
        "max_threads must be a positive integer",
        minimum=1,
        plan_version=plan_version,
    )
    cost_model = CostModel.from_dict(obj.get("cost_model"))

    raw_jobs = obj.get("jobs")
    job_items: list[Any]
    if isinstance(raw_jobs, dict):
        job_items = []
        for job_id, job_value in raw_jobs.items():
            if not isinstance(job_value, dict):
                fail(
                    INVALID_PLAN,
                    "each plan job must be an object",
                    plan_version=plan_version,
                    job_id=job_id,
                )
            job_value = dict(job_value)
            job_value.setdefault("job_id", job_id)
            job_items.append(job_value)
    elif isinstance(raw_jobs, list):
        job_items = raw_jobs
    else:
        fail(
            INVALID_PLAN,
            "plan jobs must be an object or list",
            plan_version=plan_version,
        )
    if not job_items:
        fail(INVALID_PLAN, "plan must declare at least one job", plan_version=plan_version)
    jobs: dict[str, PlanJob] = {}
    for item in job_items:
        job = _parse_plan_job(item)
        if job.job_id in jobs:
            fail(
                DUPLICATE_JOB_ID,
                "job_id must be unique in a plan",
                plan_version=plan_version,
                job_id=job.job_id,
            )
        jobs[job.job_id] = job
    _detect_job_cycle(jobs)

    raw_critical_paths = obj.get("critical_paths", {})
    critical_paths_obj = require_object(
        raw_critical_paths,
        INVALID_PLAN,
        "critical_paths must be an object",
        plan_version=plan_version,
    )
    critical_paths: dict[str, tuple[str, ...]] = {}
    for path_id, members in critical_paths_obj.items():
        path_id = require_nonempty_str(
            path_id,
            INVALID_PLAN,
            "critical path id must be a non-empty string",
            plan_version=plan_version,
        )
        members = require_list(
            members,
            INVALID_PLAN,
            "critical path members must be a list",
            plan_version=plan_version,
            critical_path_id=path_id,
        )
        parsed: list[str] = []
        for member in members:
            job_id = require_nonempty_str(
                member,
                UNKNOWN_JOB,
                "critical path member must be a known job_id",
                plan_version=plan_version,
                critical_path_id=path_id,
            )
            if job_id not in jobs:
                fail(
                    UNKNOWN_JOB,
                    "critical path references an unknown job",
                    plan_version=plan_version,
                    critical_path_id=path_id,
                    job_id=job_id,
                )
            if job_id not in parsed:
                parsed.append(job_id)
        critical_paths[path_id] = tuple(parsed)
    for job_id, job in jobs.items():
        if job.critical_path_id:
            existing = list(critical_paths.get(job.critical_path_id, ()))
            if job_id not in existing:
                existing.append(job_id)
            critical_paths[job.critical_path_id] = tuple(sorted(set(existing)))

    raw_candidates = obj.get("candidates")
    candidate_items: list[Any]
    if isinstance(raw_candidates, dict):
        candidate_items = []
        for candidate_id, candidate_value in raw_candidates.items():
            if not isinstance(candidate_value, dict):
                fail(
                    INVALID_CANDIDATE,
                    "each candidate must be an object",
                    candidate_id=candidate_id,
                )
            candidate_value = dict(candidate_value)
            candidate_value.setdefault("candidate_id", candidate_id)
            candidate_items.append(candidate_value)
    elif isinstance(raw_candidates, list):
        candidate_items = raw_candidates
    else:
        fail(
            INVALID_CANDIDATE,
            "plan must declare candidates as an object or list",
            plan_version=plan_version,
        )
    if not candidate_items:
        fail(INVALID_CANDIDATE, "plan must declare at least one candidate", plan_version=plan_version)

    candidates: list[Candidate] = []
    candidate_ids: set[str] = set()
    for item in candidate_items:
        cand_obj = require_object(
            item,
            INVALID_CANDIDATE,
            "candidate must be an object",
            plan_version=plan_version,
        )
        candidate_id = require_nonempty_str(
            cand_obj.get("candidate_id"),
            INVALID_CANDIDATE,
            "candidate_id must be a non-empty string",
            plan_version=plan_version,
        )
        if candidate_id in candidate_ids:
            fail(
                DUPLICATE_CANDIDATE,
                "candidate_id must be unique within a plan",
                plan_version=plan_version,
                candidate_id=candidate_id,
            )
        candidate_ids.add(candidate_id)
        raw_order = cand_obj.get(
            "job_order", cand_obj.get("job_ids", cand_obj.get("order"))
        )
        raw_order = require_list(
            raw_order,
            INVALID_CANDIDATE,
            "candidate job_order must be a list",
            plan_version=plan_version,
            candidate_id=candidate_id,
        )
        order: list[str] = []
        for value_job in raw_order:
            job_id = require_nonempty_str(
                value_job,
                INVALID_CANDIDATE,
                "candidate job_order entries must be job IDs",
                plan_version=plan_version,
                candidate_id=candidate_id,
            )
            if job_id not in jobs:
                fail(
                    UNKNOWN_JOB,
                    "candidate job_order references an unknown job",
                    plan_version=plan_version,
                    candidate_id=candidate_id,
                    job_id=job_id,
                )
            if job_id in order:
                fail(
                    INVALID_CANDIDATE,
                    "candidate job_order must not contain duplicates",
                    plan_version=plan_version,
                    candidate_id=candidate_id,
                    job_id=job_id,
                )
            order.append(job_id)
        if set(order) != set(jobs):
            missing = sorted(set(jobs) - set(order))
            extra = sorted(set(order) - set(jobs))
            fail(
                INVALID_CANDIDATE,
                "candidate job_order must contain every plan job exactly once",
                plan_version=plan_version,
                candidate_id=candidate_id,
                missing=missing,
                extra=extra,
            )
        interleave = require_bool(
            cand_obj.get("interleave_jobs", False),
            INVALID_CANDIDATE,
            "interleave_jobs must be a boolean",
            plan_version=plan_version,
            candidate_id=candidate_id,
        )
        overrides = cand_obj.get("jobs", {})
        overrides = require_object(
            overrides,
            INVALID_CANDIDATE,
            "candidate jobs must be an object keyed by job_id",
            plan_version=plan_version,
            candidate_id=candidate_id,
        )
        unknown_overrides = sorted(set(overrides) - set(jobs))
        if unknown_overrides:
            fail(
                UNKNOWN_JOB,
                "candidate overrides reference unknown jobs",
                plan_version=plan_version,
                candidate_id=candidate_id,
                job_ids=unknown_overrides,
            )
        resolved_jobs = {
            job_id: job.merged(overrides.get(job_id)) for job_id, job in jobs.items()
        }
        _detect_job_cycle(resolved_jobs)
        candidates.append(
            Candidate(
                candidate_id=candidate_id,
                job_order=tuple(order),
                interleave_jobs=interleave,
                jobs=resolved_jobs,
            )
        )
    candidates.sort(key=lambda item: item.candidate_id)
    return Plan(
        plan_version=plan_version,
        global_seed=global_seed,
        candidates=tuple(candidates),
        jobs=jobs,
        cost_model=cost_model,
        base_logical_clock_ms=base_clock,
        logical_clock_step_ms=clock_step,
        max_threads=max_threads,
        critical_paths=critical_paths,
    )


def parse_plan_file(path: str | Path) -> Plan:
    value = read_json_or_jsonl(path, kind="json")
    return parse_plan_object(value, source=str(path))


def parse_positive_budget(value: Any, *, name: str = "budget") -> float:
    return require_finite_number(
        value,
        BUDGET_EXHAUSTED,
        f"{name} must be a non-negative finite number",
        minimum=0.0,
    )

