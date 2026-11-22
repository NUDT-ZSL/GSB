"""Input parsing, graph validation, deterministic bytes, and fixed-point math."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from .errors import (
    E_BAD_CONFIG,
    E_BAD_PIPELINE,
    E_BAD_TASK,
    E_DIGEST_MISMATCH,
    BusinessError,
)

TIERS = ("cold", "warm", "hot")
TIER_RANK = {tier: index for index, tier in enumerate(TIERS)}
HEX_DIGITS = frozenset("0123456789abcdef")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _need_int(
    value: Any,
    label: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
    code: str = E_BAD_TASK,
    task_name: str | None = None,
    tier: str | None = None,
) -> int:
    if not _is_int(value) or (positive and value <= 0) or (nonnegative and value < 0):
        qualifier = "positive" if positive else "non-negative"
        raise BusinessError(
            code,
            f"{label} must be a {qualifier} integer",
            task_name=task_name,
            tier=tier,
            field=label,
            value=value,
        )
    return value


def _need_str(
    value: Any,
    label: str,
    *,
    code: str = E_BAD_TASK,
    task_name: str | None = None,
) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise BusinessError(
            code,
            f"{label} must be a non-empty string without NUL",
            task_name=task_name,
            field=label,
            value=value,
        )
    return value


def _read_json(path: str | Path, code: str) -> dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BusinessError(code, f"cannot read JSON: {exc}", path=str(path)) from exc
    if not isinstance(value, dict):
        raise BusinessError(code, "top-level JSON value must be an object", path=str(path))
    return value


@dataclass(frozen=True)
class TaskSpec:
    name: str
    depends_on: tuple[str, ...]
    artifact_name: str
    size_bytes: int
    written_at_ms: int
    last_hit_at_ms: int
    hit_count: int
    pinned: bool
    expected_digest: str | None = None


@dataclass(frozen=True)
class RunSpec:
    run_seq: int
    at_ms: int


@dataclass(frozen=True)
class PipelineSpec:
    name: str
    tasks: tuple[str, ...]
    runs: tuple[RunSpec, ...]


@dataclass(frozen=True)
class ConfigSpec:
    version: int
    tier_capacity_bytes: dict[str, int]
    promotion_min_hits: int
    demotion_idle_ms: int
    eviction_batch_size: int
    reclaim_byte_budget: int
    fixed_point_scale: int
    comparison_tolerance: int
    logical_clock_step_ms: int
    clock_rollback_tolerance_ms: int

    def canonical_json(self) -> str:
        return canonical_json(
            {
                "version": self.version,
                "tier_capacity_bytes": self.tier_capacity_bytes,
                "promotion_min_hits": self.promotion_min_hits,
                "demotion_idle_ms": self.demotion_idle_ms,
                "eviction_batch_size": self.eviction_batch_size,
                "reclaim_byte_budget": self.reclaim_byte_budget,
                "fixed_point_scale": self.fixed_point_scale,
                "comparison_tolerance": self.comparison_tolerance,
                "logical_clock_step_ms": self.logical_clock_step_ms,
                "clock_rollback_tolerance_ms": self.clock_rollback_tolerance_ms,
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self.canonical_json())


@dataclass(frozen=True)
class Dataset:
    tasks: tuple[TaskSpec, ...]
    pipelines: tuple[PipelineSpec, ...]
    config: ConfigSpec


@dataclass
class ArtifactPlan:
    task: TaskSpec
    digest: str
    size_bytes: int


@dataclass
class ArithmeticOperation:
    operation: str
    scale: int
    rounding: str
    raw_value: Any
    converted_value: int
    error_num: int
    cumulative_error_num: int
    details: dict[str, Any] = field(default_factory=dict)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_tasks(path: str | Path) -> tuple[TaskSpec, ...]:
    doc = _read_json(path, E_BAD_TASK)
    if "schema_version" in doc and doc["schema_version"] != 1:
        raise BusinessError(E_BAD_TASK, "unsupported task schema_version", value=doc["schema_version"])
    raw_tasks = doc.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise BusinessError(E_BAD_TASK, "tasks must be a non-empty array", field="tasks")

    tasks: list[TaskSpec] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_tasks):
        if not isinstance(raw, dict):
            raise BusinessError(E_BAD_TASK, "task entry must be an object", index=index)
        name = _need_str(raw.get("name"), "name")
        if name in seen:
            raise BusinessError(E_BAD_TASK, "duplicate task name", task_name=name)
        seen.add(name)
        depends_raw = raw.get("depends_on", [])
        if not isinstance(depends_raw, list):
            raise BusinessError(
                E_BAD_TASK, "depends_on must be an array", task_name=name, field="depends_on"
            )
        depends: list[str] = []
        for dep in depends_raw:
            dep_name = _need_str(dep, "depends_on entry", task_name=name)
            if dep_name == name:
                raise BusinessError(E_BAD_TASK, "task cannot depend on itself", task_name=name)
            if dep_name in depends:
                raise BusinessError(
                    E_BAD_TASK, "duplicate dependency", task_name=name, dependency=dep_name
                )
            depends.append(dep_name)

        artifact_name = _need_str(raw.get("artifact_name"), "artifact_name", task_name=name)
        size_bytes = _need_int(
            raw.get("size_bytes"), "size_bytes", positive=True, task_name=name
        )
        written_at_ms = _need_int(
            raw.get("written_at_ms"), "written_at_ms", nonnegative=True, task_name=name
        )
        last_hit_at_ms = _need_int(
            raw.get("last_hit_at_ms"), "last_hit_at_ms", nonnegative=True, task_name=name
        )
        hit_count = _need_int(
            raw.get("hit_count"), "hit_count", nonnegative=True, task_name=name
        )
        if last_hit_at_ms < written_at_ms:
            raise BusinessError(
                E_BAD_TASK,
                "last_hit_at_ms must not be earlier than written_at_ms",
                task_name=name,
            )
        pinned = raw.get("pinned")
        if not isinstance(pinned, bool):
            raise BusinessError(
                E_BAD_TASK, "pinned must be a boolean", task_name=name, field="pinned"
            )
        expected_digest = raw.get("expected_sha256")
        if expected_digest is not None:
            if (
                not isinstance(expected_digest, str)
                or len(expected_digest) != 64
                or any(ch not in HEX_DIGITS for ch in expected_digest.lower())
            ):
                raise BusinessError(
                    E_BAD_TASK,
                    "expected_sha256 must be a 64-character hex digest",
                    task_name=name,
                )
            expected_digest = expected_digest.lower()
        tasks.append(
            TaskSpec(
                name=name,
                depends_on=tuple(depends),
                artifact_name=artifact_name,
                size_bytes=size_bytes,
                written_at_ms=written_at_ms,
                last_hit_at_ms=last_hit_at_ms,
                hit_count=hit_count,
                pinned=pinned,
                expected_digest=expected_digest,
            )
        )

    for task in tasks:
        for dep in task.depends_on:
            if dep not in seen:
                raise BusinessError(
                    E_BAD_TASK,
                    "dependency does not exist in task manifest",
                    task_name=task.name,
                    dependency=dep,
                )
    _detect_cycles(tasks)
    return tuple(tasks)


def _detect_cycles(tasks: Iterable[TaskSpec]) -> None:
    graph = {task.name: task.depends_on for task in tasks}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(name: str) -> None:
        marker = state.get(name, 0)
        if marker == 2:
            return
        if marker == 1:
            start = stack.index(name)
            cycle = stack[start:] + [name]
            raise BusinessError(
                E_BAD_TASK, "dependency cycle detected", task_name=name, cycle=cycle
            )
        state[name] = 1
        stack.append(name)
        for dep in graph[name]:
            visit(dep)
        stack.pop()
        state[name] = 2

    for name in sorted(graph):
        visit(name)


def read_pipelines(path: str | Path) -> tuple[PipelineSpec, ...]:
    doc = _read_json(path, E_BAD_PIPELINE)
    if "schema_version" in doc and doc["schema_version"] != 1:
        raise BusinessError(
            E_BAD_PIPELINE, "unsupported pipeline schema_version", value=doc["schema_version"]
        )
    raw_pipelines = doc.get("pipelines")
    if not isinstance(raw_pipelines, list) or not raw_pipelines:
        raise BusinessError(E_BAD_PIPELINE, "pipelines must be a non-empty array")

    seen: set[str] = set()
    pipelines: list[PipelineSpec] = []
    for index, raw in enumerate(raw_pipelines):
        if not isinstance(raw, dict):
            raise BusinessError(E_BAD_PIPELINE, "pipeline entry must be an object", index=index)
        name = _need_str(raw.get("name"), "pipeline name", code=E_BAD_PIPELINE)
        if name in seen:
            raise BusinessError(E_BAD_PIPELINE, "duplicate pipeline name", pipeline=name)
        seen.add(name)
        tasks_raw = raw.get("tasks")
        if not isinstance(tasks_raw, list) or not tasks_raw:
            raise BusinessError(
                E_BAD_PIPELINE, "pipeline tasks must be a non-empty array", pipeline=name
            )
        tasks: list[str] = []
        for task_raw in tasks_raw:
            task_name = _need_str(task_raw, "pipeline task", code=E_BAD_PIPELINE)
            if task_name in tasks:
                raise BusinessError(
                    E_BAD_PIPELINE,
                    "pipeline references the same task more than once",
                    pipeline=name,
                    task_name=task_name,
                )
            tasks.append(task_name)

        runs_raw = raw.get("runs")
        if not isinstance(runs_raw, list) or not runs_raw:
            raise BusinessError(
                E_BAD_PIPELINE, "pipeline runs must be a non-empty array", pipeline=name
            )
        runs: list[RunSpec] = []
        seen_run_seq: set[int] = set()
        for run_raw in runs_raw:
            if _is_int(run_raw):
                run_seq = len(runs) + 1
                at_ms = _need_int(
                    run_raw, "run clock", nonnegative=True, code=E_BAD_PIPELINE
                )
            elif isinstance(run_raw, dict):
                run_seq = _need_int(
                    run_raw.get("run_seq"),
                    "run_seq",
                    positive=True,
                    code=E_BAD_PIPELINE,
                )
                at_ms = _need_int(
                    run_raw.get("at_ms"),
                    "run at_ms",
                    nonnegative=True,
                    code=E_BAD_PIPELINE,
                )
            else:
                raise BusinessError(
                    E_BAD_PIPELINE, "run must be an integer clock or object", pipeline=name
                )
            if run_seq in seen_run_seq:
                raise BusinessError(
                    E_BAD_PIPELINE,
                    "duplicate run_seq in pipeline",
                    pipeline=name,
                    run_seq=run_seq,
                )
            seen_run_seq.add(run_seq)
            runs.append(RunSpec(run_seq=run_seq, at_ms=at_ms))
        runs.sort(key=lambda item: item.run_seq)
        pipelines.append(
            PipelineSpec(name=name, tasks=tuple(tasks), runs=tuple(runs))
        )
    return tuple(pipelines)


def read_config(path: str | Path) -> ConfigSpec:
    doc = _read_json(path, E_BAD_CONFIG)
    # "schema_version" is accepted as an alias for the capacity config version.
    version_raw = doc.get("version", doc.get("schema_version"))
    version = _need_int(version_raw, "version", positive=True, code=E_BAD_CONFIG)
    tiers_raw = doc.get("tier_capacity_bytes", doc.get("tiers"))
    if not isinstance(tiers_raw, dict):
        raise BusinessError(
            E_BAD_CONFIG, "tier_capacity_bytes must be an object", field="tier_capacity_bytes"
        )
    capacities: dict[str, int] = {}
    for tier in TIERS:
        raw_capacity = tiers_raw.get(tier)
        if isinstance(raw_capacity, dict):
            raw_capacity = raw_capacity.get("capacity_bytes")
        capacities[tier] = _need_int(
            raw_capacity,
            f"{tier} capacity",
            positive=True,
            code=E_BAD_CONFIG,
            tier=tier,
        )
    promotion_min_hits = _need_int(
        doc.get("promotion_min_hits"),
        "promotion_min_hits",
        positive=True,
        code=E_BAD_CONFIG,
    )
    demotion_idle_ms = _need_int(
        doc.get("demotion_idle_ms"),
        "demotion_idle_ms",
        positive=True,
        code=E_BAD_CONFIG,
    )
    eviction_batch_size = _need_int(
        doc.get("eviction_batch_size"),
        "eviction_batch_size",
        positive=True,
        code=E_BAD_CONFIG,
    )
    reclaim_byte_budget = _need_int(
        doc.get("reclaim_byte_budget"),
        "reclaim_byte_budget",
        positive=True,
        code=E_BAD_CONFIG,
    )
    fixed_point_scale = _need_int(
        doc.get("fixed_point_scale"),
        "fixed_point_scale",
        positive=True,
        code=E_BAD_CONFIG,
    )
    comparison_tolerance = _need_int(
        doc.get("comparison_tolerance"),
        "comparison_tolerance",
        nonnegative=True,
        code=E_BAD_CONFIG,
    )
    clock_rollback_tolerance_ms = _need_int(
        doc.get("clock_rollback_tolerance_ms", comparison_tolerance),
        "clock_rollback_tolerance_ms",
        nonnegative=True,
        code=E_BAD_CONFIG,
    )
    logical_clock_step_ms = _need_int(
        doc.get("logical_clock_step_ms"),
        "logical_clock_step_ms",
        positive=True,
        code=E_BAD_CONFIG,
    )
    return ConfigSpec(
        version=version,
        tier_capacity_bytes=capacities,
        promotion_min_hits=promotion_min_hits,
        demotion_idle_ms=demotion_idle_ms,
        eviction_batch_size=eviction_batch_size,
        reclaim_byte_budget=reclaim_byte_budget,
        fixed_point_scale=fixed_point_scale,
        comparison_tolerance=comparison_tolerance,
        logical_clock_step_ms=logical_clock_step_ms,
        clock_rollback_tolerance_ms=clock_rollback_tolerance_ms,
    )


def load_dataset(tasks_path: str | Path, pipelines_path: str | Path, config_path: str | Path) -> Dataset:
    tasks = read_tasks(tasks_path)
    pipelines = read_pipelines(pipelines_path)
    config = read_config(config_path)
    return Dataset(tasks=tasks, pipelines=pipelines, config=config)


def iter_artifact_bytes(task_name: str, written_at_ms: int, size_bytes: int) -> Iterator[bytes]:
    """Generate stable bytes without allocating the whole artifact."""
    remaining = size_bytes
    counter = 0
    prefix = f"ci-cache-artifact-v1\x00{task_name}\x00{written_at_ms}\x00".encode("utf-8")
    while remaining:
        block = hashlib.sha256(prefix + str(counter).encode("ascii")).digest()
        take = min(remaining, len(block))
        yield block[:take]
        remaining -= take
        counter += 1


def artifact_digest(task_name: str, written_at_ms: int, size_bytes: int) -> str:
    digest = hashlib.sha256()
    for block in iter_artifact_bytes(task_name, written_at_ms, size_bytes):
        digest.update(block)
    return digest.hexdigest()


def plan_artifacts(tasks: Iterable[TaskSpec]) -> tuple[ArtifactPlan, ...]:
    plans: list[ArtifactPlan] = []
    for task in tasks:
        digest = artifact_digest(task.name, task.written_at_ms, task.size_bytes)
        if task.expected_digest is not None and digest != task.expected_digest:
            raise BusinessError(
                E_DIGEST_MISMATCH,
                "generated artifact digest does not match expected_sha256",
                task_name=task.name,
                expected_digest=task.expected_digest,
                actual_digest=digest,
            )
        plans.append(ArtifactPlan(task=task, digest=digest, size_bytes=task.size_bytes))
    return tuple(plans)


def fp_decimal(numerator: int, scale: int) -> str:
    sign = "-" if numerator < 0 else ""
    whole, fraction = divmod(abs(numerator), scale)
    digits = len(str(scale)) - 1
    if scale != 10**digits:
        # A non-power-of-ten fixed point still gets a stable, informative
        # decimal projection; integer numerators remain authoritative.
        digits = 9
        fraction = (fraction * 10**digits) // scale
    return f"{sign}{whole}.{fraction:0{digits}d}"


def fp_value(numerator: int, scale: int) -> dict[str, Any]:
    return {"numerator": numerator, "scale": scale, "decimal": fp_decimal(numerator, scale)}


def score_components(
    idle_ms: int, hit_count: int, max_hit_count: int, config: ConfigSpec, scale: int | None = None
) -> dict[str, Any]:
    """Fixed-point eviction score implementing stable idle-then-hit ordering.

    One additional idle millisecond outweighs every possible hit-count
    difference, so larger idle time always wins first; hit deficit is the
    deterministic secondary key.
    """
    used_scale = config.fixed_point_scale if scale is None else scale
    idle_weight = max_hit_count * config.logical_clock_step_ms + 1
    idle_units = idle_ms * idle_weight * used_scale
    hit_deficit = max_hit_count - hit_count
    hit_units = hit_deficit * config.logical_clock_step_ms * used_scale
    score = idle_units + hit_units

    cumulative = 0
    operations: list[ArithmeticOperation] = []
    for label, value, raw in (
        ("idle_mul_weight_mul_scale", idle_units, idle_ms),
        ("hit_deficit_mul_step_mul_scale", hit_units, hit_deficit),
        ("score_add", score, [idle_units, hit_units]),
    ):
        error_num = 0
        cumulative += error_num
        operations.append(
            ArithmeticOperation(
                operation=label,
                scale=used_scale,
                rounding="exact",
                raw_value=raw,
                converted_value=value,
                error_num=error_num,
                cumulative_error_num=cumulative,
            )
        )
    return {
        "idle_ms": idle_ms,
        "hit_count": hit_count,
        "max_hit_count": max_hit_count,
        "idle_units": idle_units,
        "hit_penalty_units": hit_units,
        "score": score,
        "score_fp": fp_value(score, used_scale),
        "error_bound": fp_value(cumulative, used_scale),
        "operations": operations,
    }


def high_precision_order(candidates: list[dict[str, Any]], config: ConfigSpec) -> list[str]:
    """Recompute ordering using 1000x the configured scale."""
    high_scale = config.fixed_point_scale * 1000
    scored: list[tuple[int, str]] = []
    for candidate in candidates:
        components = score_components(
            candidate["idle_ms"],
            candidate["hit_count"],
            candidate["max_hit_count"],
            config,
            high_scale,
        )
        scored.append((components["score"], candidate["task_name"]))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [task_name for _, task_name in scored]
